from __future__ import annotations

from safety import h3grid


def test_cells_for_point_covers_every_stored_resolution():
    cells = h3grid.cells_for_point(39.9526, -75.1652)
    assert set(cells) == {8, 9, 10}
    for res, cell in cells.items():
        assert h3grid.is_valid_cell(cell)
        assert h3grid.cell_resolution(cell) == res


def test_grid_disk_k1():
    cell = h3grid.cell_for(39.9526, -75.1652, 8)
    disk = h3grid.grid_disk(cell, 1)
    assert len(disk) == 7
    assert cell in disk


def test_invalid_cell():
    assert h3grid.is_valid_cell("not-a-cell") is False
