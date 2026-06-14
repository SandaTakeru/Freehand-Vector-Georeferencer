<p align="center">
  <img src="icon.png" alt="Plugin Icon" width="120"/>
</p>

# Freehand Vector Georeferencer

An intuitive georeferencing (coordinate-correction) tool for **vector** layers in QGIS.
Grab an old node and release it at the new position to add control points (GCPs),
preview the **Helmert / Affine** transform in real time, and apply the result.

## Demo


https://github.com/user-attachments/assets/aa6e721d-2d70-482f-87b0-d96f6089c474



## Features

- **Mouse-only GCPs** — no numeric input. Press an old node, release at the new
  position. Click an existing GCP to include/exclude it; drag it to move its target.
- **Snapping** — sources and destinations snap to vertices of any visible vector
  layer (self-snap supported); a source can also be a free point.
- **Transforms** — Helmert (fixed scale or with scale) and Affine.
- **Live preview** while dragging, with adjustable preview FPS and vertex
  decimation for heavy data.
- **Error feedback** — per-GCP X/Y error, RMS, standard deviation and scale factor;
  residuals colored green → red; sortable list.
- **Apply** as a new layer, as features added to the current layer, or as an
  in-place edit (add / edit run in edit mode, so `Ctrl+Z` undoes them).
- **Reusable GCPs** — save / load as CSV to reuse the same transform on another
  layer in the same CRS; a temporary CSV is written on every apply.

## Note on CRS

This tool applies a plain 2D affine transform and does **not** reproject or apply
geodetic / scale-factor correction. Keep the target layer CRS and the project CRS
identical (a warning is shown when they differ).

## Acknowledgment

The interactive UI of this plugin — the map tool, the rubber-band preview and the
"grab a node and move it" feel — is inspired by the
**[Freehand Raster Georeferencer](https://github.com/gvellut/FreehandRasterGeoreferencer)**
plugin by **Guilhem Vellut**. Many thanks for that great earlier work.

## License

GNU General Public License v2.0 or later. See [LICENSE](LICENSE).
