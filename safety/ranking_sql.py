"""The two all-hours rankings, as SQL shared by the ETL and the serving layer.

The ETL writes these into gold.cell_activity and gold.cell_safety for every
precomputed window (safety.etl.gold). The API runs the very same statements for
a date range nobody precomputed, summing gold.cell_daily instead
(safety.api.repository, the range path). One copy of the arithmetic, so a range
that happens to equal a built window ranks every cell exactly as the stored
window does -- tests/test_gold_invariants.py holds the two to that.

A neutral module for the same reason as safety.h3grid: the serving layer keeps
no import dependency on the ETL package.

Each statement is a plain SELECT whose columns are named and typed like the gold
table it feeds, with one placeholder for the per-cell sums it ranks:

* ACTIVITY_RANKED  -- ``{counts}``: h3_index, c_all, c_violent, c_property,
  c_quality_of_life, c_other.
  Parameters: source_id, h3_res, time_window, window_start, window_end,
  categories.
* SAFETY_RANKED    -- ``{weighted}``: h3_index, n_violent, n_non_violent,
  w_violent, w_non_violent.
  Parameters: those above less categories, plus scheme, eb_prior, self_weight,
  per_capita, jobs_weight (scheme_params).
"""

from __future__ import annotations

from typing import Any, Mapping

ACTIVITY_RANKED = """
WITH counts AS (
{counts}
),
universe AS (
    SELECT h3_index, area_km2
    FROM gold.cell_geometry
    WHERE source_id = %(source_id)s AND h3_res = %(h3_res)s
),
joined AS (
    SELECT
        u.h3_index,
        u.area_km2,
        COALESCE(c.c_all, 0)             AS c_all,
        COALESCE(c.c_violent, 0)         AS c_violent,
        COALESCE(c.c_property, 0)        AS c_property,
        COALESCE(c.c_quality_of_life, 0) AS c_quality_of_life,
        COALESCE(c.c_other, 0)           AS c_other
    FROM universe u
    LEFT JOIN counts c USING (h3_index)
),
unpivoted AS (
    SELECT h3_index, area_km2, category, n
    FROM joined
    CROSS JOIN LATERAL (VALUES
        ('all',             c_all),
        ('violent',         c_violent),
        ('property',        c_property),
        ('quality_of_life', c_quality_of_life),
        ('other',           c_other)
    ) AS v(category, n)
    -- Narrowed at resolution 10; see ACTIVITY_CATEGORIES. Filtered here, ahead
    -- of the window functions below, which is both cheaper and safe: every
    -- percentile partitions by category, so dropping whole categories cannot
    -- move the ranking of the ones that remain.
    WHERE category = ANY(%(categories)s)
),
ranked AS (
    SELECT
        h3_index,
        category,
        n,
        n::double precision / area_km2 AS density,
        -- S3.3: position within this city's own distribution for this window
        -- and category. Ties (notably the block of zero-incident cells) all
        -- receive the same, lowest, percentile.
        percent_rank() OVER (PARTITION BY category ORDER BY n::double precision / area_km2) AS pr,
        rank()         OVER (PARTITION BY category ORDER BY n::double precision / area_km2 DESC) AS rnk,
        count(*)       OVER (PARTITION BY category) AS cell_total
    FROM unpivoted
)
SELECT
    %(source_id)s::text       AS source_id,
    h3_index,
    %(h3_res)s::smallint      AS h3_res,
    %(time_window)s::text     AS time_window,
    category,
    %(window_start)s::date    AS window_start,
    %(window_end)s::date      AS window_end,
    n::integer                AS incident_count,
    density                   AS incidents_per_km2,
    rnk::integer              AS city_rank,
    cell_total::integer       AS city_cell_total,
    pr                        AS percentile,
    CASE
        -- Tier 0 is "nothing was reported here", which is a different
        -- statement from "this is the quietest fifth of the city" (S2).
        WHEN n = 0     THEN 0
        WHEN pr < 0.20 THEN 1
        WHEN pr < 0.40 THEN 2
        WHEN pr < 0.60 THEN 3
        WHEN pr < 0.80 THEN 4
        ELSE 5
    END::smallint             AS activity_tier,
    now()                     AS refreshed_at
FROM ranked
"""

SAFETY_RANKED = """
WITH weighted AS (
{weighted}
),
universe AS (
    -- Area and exposure travel together from here down. The ranking divides by
    -- whichever one the scheme names; area_km2 stays in scope regardless,
    -- because weighted_per_km2 is still written and is still true.
    --
    -- Area is the poorer denominator and that is the point of the exposure
    -- column: a cell's weighted total scales with how many people are in it,
    -- so ranking on area alone reports where the city is busy as much as where
    -- it is dangerous.
    SELECT
        g.h3_index,
        g.area_km2,
        CASE WHEN %(per_capita)s
             THEN COALESCE(e.residents, 0) + COALESCE(e.jobs, 0) * %(jobs_weight)s
             ELSE g.area_km2
        END AS exposure
    FROM gold.cell_geometry g
    LEFT JOIN gold.cell_exposure e
           ON e.source_id = g.source_id
          AND e.h3_index  = g.h3_index
          AND e.h3_res    = g.h3_res
    WHERE g.source_id = %(source_id)s AND g.h3_res = %(h3_res)s
),
joined AS (
    SELECT
        u.h3_index,
        u.area_km2,
        u.exposure,
        COALESCE(x.n_violent, 0)     AS n_violent,
        COALESCE(x.n_non_violent, 0) AS n_non_violent,
        COALESCE(x.w_violent, 0)     AS w_violent,
        COALESCE(x.w_non_violent, 0) AS w_non_violent
    FROM universe u
    LEFT JOIN weighted x USING (h3_index)
),
unpivoted AS (
    SELECT h3_index, area_km2, exposure, track, n, w
    FROM joined
    CROSS JOIN LATERAL (VALUES
        ('violent',     n_violent,     w_violent),
        ('non_violent', n_non_violent, w_non_violent)
    ) AS v(track, n, w)
),
city AS (
    -- The rate each cell is shrunk toward: this track's citywide weighted
    -- offense per unit of exposure.
    SELECT track, sum(w) / NULLIF(sum(exposure), 0) AS city_rate
    FROM unpivoted
    GROUP BY track
),
adjusted AS (
    SELECT
        u.h3_index, u.area_km2, u.exposure, u.track, u.n, u.w,
        -- Poisson-gamma posterior rate: the cell's own weighted total plus
        -- eb_prior worth of citywide-average offense, over its own exposure
        -- plus that same prior.
        --
        -- The prior has to be an exposure rather than a count. A cell with no
        -- incidents was still watched for the whole window, so zero is evidence
        -- of a low rate, not missing information -- shrinking by n/(n+k) instead
        -- sends every empty cell to the citywide mean, which ranked a cell with
        -- six assaults safer than a cell with none.
        --
        -- With a population denominator the prior earns a second job: it is what
        -- keeps a cell whose ambient population rounds to nothing from dividing
        -- by zero. As exposure falls away the posterior tends to
        -- city_rate + w/prior, which is bounded. That is why the airport and the
        -- middle of Fairmount Park can be ranked at all rather than pinned to
        -- the bottom by arithmetic.
        (u.w + COALESCE(c.city_rate, 0) * %(eb_prior)s)
            / NULLIF(u.exposure + %(eb_prior)s, 0) AS adj
    FROM unpivoted u
    JOIN city c USING (track)
),
neighbor_mean AS (
    -- Grouped join rather than a correlated LATERAL, for the same reason the
    -- hourly build uses one: the LATERAL form re-scans `adjusted` once per row.
    -- At resolution 8 that is 1,102 rows and costs about two seconds; at
    -- resolution 10 it is 49,540 and cost two minutes per window, which was
    -- most of a Philadelphia gold refresh and would have been most of six.
    -- Computing every cell's neighbour mean in one pass is the same arithmetic.
    SELECT nbr.h3_index, x.track, avg(x.adj) AS mean_adj
    FROM gold.cell_neighbor nbr
    JOIN adjusted x ON x.h3_index = nbr.neighbor_h3
    WHERE nbr.source_id = %(source_id)s AND nbr.h3_res = %(h3_res)s
    GROUP BY 1, 2
),
blended AS (
    SELECT
        a.h3_index, a.area_km2, a.exposure, a.track, a.n, a.w, a.adj,
        -- Risk does not stop at a hexagon edge. A cell with no in-universe
        -- neighbours keeps its own value rather than being pulled toward zero.
        CASE WHEN nm.mean_adj IS NULL THEN a.adj
             ELSE %(self_weight)s * a.adj + (1 - %(self_weight)s) * nm.mean_adj
        END AS smoothed
    FROM adjusted a
    LEFT JOIN neighbor_mean nm
           ON nm.h3_index = a.h3_index AND nm.track = a.track
),
ranked AS (
    SELECT
        b.*,
        -- Hazen midrank, ordered so the *lowest* weighted total scores highest:
        -- 1.0 is the safest cell. Tie blocks sit at the centre of their own
        -- range, so the score never degenerates to exactly 0 or 1.
        (
            (rank() OVER (PARTITION BY b.track ORDER BY b.smoothed DESC)
             + (count(*) OVER (PARTITION BY b.track, b.smoothed) - 1) / 2.0)
            - 0.5
        ) / count(*) OVER (PARTITION BY b.track) AS pct,
        rank()   OVER (PARTITION BY b.track ORDER BY b.smoothed DESC) AS rnk,
        count(*) OVER (PARTITION BY b.track) AS cell_total
    FROM blended b
)
SELECT
    %(source_id)s::text       AS source_id,
    h3_index,
    %(h3_res)s::smallint      AS h3_res,
    %(time_window)s::text     AS time_window,
    track,
    %(scheme)s::text          AS scheme_version,
    %(window_start)s::date    AS window_start,
    %(window_end)s::date      AS window_end,
    n::integer                AS incident_count,
    w::double precision       AS weighted_total,
    (w / area_km2)::double precision AS weighted_per_km2,
    smoothed::double precision       AS smoothed_per_km2,
    -- Both NULL for an area scheme: that row's denominator is area_km2, and a
    -- per-1,000-people figure would be a number it never computed.
    (CASE WHEN %(per_capita)s THEN exposure END)::double precision AS exposure,
    (CASE WHEN %(per_capita)s THEN w / NULLIF(exposure / 1000.0, 0) END)::double precision
                              AS weighted_per_1k,
    pct::double precision     AS safety_percentile,
    rnk::integer              AS safety_rank,
    cell_total::integer       AS city_cell_total,
    CASE
        -- Tier 0 is "nothing of this track was reported here", which is not the
        -- same claim as "this is among the safest quarter of the city": an
        -- absence of reports can be an absence of reporting (S13).
        WHEN n = 0      THEN 0
        WHEN pct < 0.25 THEN 1
        WHEN pct < 0.50 THEN 2
        WHEN pct < 0.75 THEN 3
        ELSE 4
    END::smallint             AS safety_tier,
    now()                     AS refreshed_at
FROM ranked
"""


def scheme_params(scheme: Mapping[str, Any]) -> dict[str, Any]:
    """SAFETY_RANKED's scheme parameters from a reference.severity_scheme row.

    Mirrors safety.etl.gold.Scheme: the prior is in whichever unit the scheme's
    denominator is measured in (km2 of citywide-average evidence for an area
    scheme, ambient persons for a per-capita one).
    """
    per_capita = scheme["exposure_kind"] == "ambient_population"
    prior = scheme["eb_prior_persons"] if per_capita else scheme["eb_prior_km2"]
    if prior is None:
        raise ValueError(
            f"scheme '{scheme['scheme_version']}' is per-capita but has no eb_prior_persons"
        )
    return {
        "scheme": scheme["scheme_version"],
        "eb_prior": prior,
        "self_weight": scheme["self_weight"],
        "per_capita": per_capita,
        "jobs_weight": scheme["jobs_weight"],
    }
