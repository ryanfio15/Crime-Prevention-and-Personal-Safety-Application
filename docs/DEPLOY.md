# Deploying to the home server (moved)

This guide has been merged into [`DEPLOYMENT.md`](DEPLOYMENT.md). The file stays because code comments and
applied (checksummed, so immutable) migrations cite it by section name. Each old section is now here:

| Old section | Now |
|---|---|
| What runs where, Release layout | [DEPLOYMENT.md → Release layout](DEPLOYMENT.md#release-layout), [ARCHITECTURE.md](ARCHITECTURE.md) |
| How a deploy works | [DEPLOYMENT.md → How a deploy works](DEPLOYMENT.md#how-a-deploy-works) |
| Migrations must be backward compatible | [DEPLOYMENT.md → Migrations during a deploy](DEPLOYMENT.md#migrations-during-a-deploy) and [Migration checksums](DEPLOYMENT.md#migration-checksums-and-the-migrate-lock) |
| Day to day | [DEPLOYMENT.md → Routine deploys](DEPLOYMENT.md#routine-deploys) |
| Setting up an instance (the full order) | [DEPLOYMENT.md → From-zero setup](DEPLOYMENT.md#from-zero-setup) |
| nginx sites | [DEPLOYMENT.md → nginx sites](DEPLOYMENT.md#nginx-sites) |
| Moving an instance to the release layout | [DEPLOYMENT.md → Moving an instance to the release layout](DEPLOYMENT.md#moving-an-instance-to-the-release-layout) |
| Recommended GitHub settings and access | [DEPLOYMENT.md → Branch protection and GitHub settings](DEPLOYMENT.md#branch-protection-and-github-settings) |
| Operating the data (records withdrawn upstream, restoring deleted rows) | [DEPLOYMENT.md → Operating the data](DEPLOYMENT.md#operating-the-data) |
| Backups | [OPERATIONS.md → Backups](OPERATIONS.md#backups), [DEPLOYMENT.md → Backups](DEPLOYMENT.md#backups) |
| Database roles | [DEPLOYMENT.md → Database roles](DEPLOYMENT.md#database-roles) |
| OS users | [DEPLOYMENT.md → OS users](DEPLOYMENT.md#os-users) |
| If something goes wrong | [OPERATIONS.md](OPERATIONS.md), [README → Troubleshooting](../readme.md#14-troubleshooting) |
| Notes for developers | [DEPLOYMENT.md → Notes for developers](DEPLOYMENT.md#notes-for-developers) |

One correction: a deploy whose migration hits `lock_timeout` during the nightly backup is **skipped**, not retried as
this guide used to say ([Backups](DEPLOYMENT.md#backups)).
