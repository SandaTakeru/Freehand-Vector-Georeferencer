# -*- coding: utf-8 -*-
'''Core engine for a georeferencing session.

Holds the GCPs (control points), computes the transform matrix, draws the
preview, shows the markers, computes residuals and builds the output layer.
The UI (dock) drives this class.
'''

import csv
import time

import numpy as np

from qgis.core import (
    Qgis,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtGui import QColor

from . import transform


# Max number of anchor vertices for snapping (decimated for heavy data).
MAX_ANCHORS = 5000
# Total-vertex threshold above which the preview is simplified.
SIMPLIFY_THRESHOLD = 20000

# Update interval (ms) and simplification factor per preview quality.
QUALITY = {
    'High (30fps)': {'interval': 33, 'simplify': 0.5},
    'Medium (10fps)': {'interval': 100, 'simplify': 1.0},
    'Light (4fps)': {'interval': 250, 'simplify': 2.0},
    'Minimum (1fps)': {'interval': 1000, 'simplify': 3.0},
}
DEFAULT_QUALITY = 'Medium (10fps)'

# Endpoints of the residual color ramp (green -> yellow -> red).
_COLOR_GREEN = (0, 180, 0)
_COLOR_YELLOW = (230, 200, 0)
_COLOR_RED = (220, 0, 0)


def _lerp(a, b, f):
    return int(round(a + (b - a) * f))


def residual_color(mag, max_mag):
    '''Returns a color interpolated linearly green -> yellow -> red over
    0..max_mag.

    Equal mag values get the same color. If max_mag is 0 (all equal, no
    residual) the color is green.
    '''
    if max_mag <= 1e-12:
        t = 0.0
    else:
        t = min(max(mag / max_mag, 0.0), 1.0)
    if t <= 0.5:
        f = t / 0.5
        c0, c1 = _COLOR_GREEN, _COLOR_YELLOW
    else:
        f = (t - 0.5) / 0.5
        c0, c1 = _COLOR_YELLOW, _COLOR_RED
    return QColor(_lerp(c0[0], c1[0], f),
                  _lerp(c0[1], c1[1], f),
                  _lerp(c0[2], c1[2], f))


class Gcp(object):
    '''A single control point. src is the source coordinate, dst is the target.'''

    def __init__(self, src, dst, active=True):
        self.src = (float(src[0]), float(src[1]))
        self.dst = (float(dst[0]), float(dst[1]))
        self.active = active


class GeorefSession(object):
    def __init__(self, iface, layer, scope, mode, lock_scale, quality,
                 on_update):
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.layer = layer
        self.scope = scope            # 'single' / 'selected' / 'all'
        self.mode = mode              # 'helmert' / 'affine'
        self.lock_scale = lock_scale
        self.quality = quality if quality in QUALITY else DEFAULT_QUALITY
        self.on_update = on_update    # callback notifying the UI of stats/table

        self.gcps = []
        self.matrix = transform._identity()
        self._draw_matrix = transform._identity()  # matrix actually drawn (provisional while dragging)
        self._dragging = False        # render a lighter preview while dragging

        self._geom_type = QgsWkbTypes.geometryType(layer.wkbType())
        self._features = []           # list of (fid, QgsGeometry): source geometries to transform
        self._anchors = np.zeros((0, 2))
        self._anchor_fids = np.zeros(0, dtype=np.int64)  # feature id of each anchor vertex
        self._total_vertices = 0
        self._layer_was_visible = True
        self._layer_hidden = False

        # Single-feature mode: keep all features as candidates and narrow down
        # to the feature of the first grabbed node.
        self._candidates = []         # all candidate (fid, QgsGeometry)
        self._locked_fid = None       # confirmed target feature id (single mode)
        self._last_snap_fid = None    # feature id of the node last grabbed by snap_source

        # Rubber band for the preview.
        self.rb_preview = QgsRubberBand(self.canvas, self._geom_type)
        self.rb_preview.setColor(QColor(30, 120, 255, 180))
        self.rb_preview.setFillColor(QColor(30, 120, 255, 40))
        self.rb_preview.setWidth(2)
        # Static preview that keeps the non-target features at their original
        # position in single-feature mode.
        self.rb_static = QgsRubberBand(self.canvas, self._geom_type)
        self.rb_static.setColor(QColor(120, 120, 120, 160))
        self.rb_static.setFillColor(QColor(120, 120, 120, 30))
        self.rb_static.setWidth(1)
        # Residual vectors (transformed src -> dst).
        self.rb_residual = QgsRubberBand(self.canvas, Qgis.GeometryType.Line)
        self.rb_residual.setColor(QColor(255, 0, 0, 200))
        self.rb_residual.setWidth(1)

        self.markers = []             # one QgsVertexMarker per GCP

        # Throttling of preview updates.
        self._last_update = 0.0
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._do_preview)

        self._build_scope()

    # ------------------------------------------------------------------
    # Initialization / collecting the target features
    # ------------------------------------------------------------------
    def _build_scope(self):
        '''Collects the target features and snapping anchors per the scope.

        In single-feature mode all features are loaded as candidates at start,
        and the feature of the first grabbed node is confirmed as the target
        (narrowed down by _lock_single).
        '''
        if self.scope == 'all':
            feats = list(self.layer.getFeatures())
        elif self.scope == 'selected':
            feats = list(self.layer.getSelectedFeatures())
        else:  # single: keep all features as candidates
            feats = list(self.layer.getFeatures())

        collected = []
        for f in feats:
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            collected.append((f.id(), QgsGeometry(g)))

        if self.scope == 'single':
            # Keep all features as candidates; preview them all until confirmed.
            self._candidates = collected
            self._features = list(collected)
        else:
            self._candidates = []
            self._features = collected

        self._rebuild_anchors()

    def _rebuild_anchors(self):
        '''Rebuilds the snapping anchors (vertices and feature ids) from
        the current _features.'''
        pts = []
        fids = []
        for fid, g in self._features:
            for v in g.vertices():
                pts.append((v.x(), v.y()))
                fids.append(fid)
        self._total_vertices = len(pts)
        if pts:
            arr = np.array(pts, dtype=float)
            fid_arr = np.array(fids, dtype=np.int64)
            # Decimate by stride if there are too many (lighter snap candidates).
            if len(arr) > MAX_ANCHORS:
                step = int(np.ceil(len(arr) / MAX_ANCHORS))
                arr = arr[::step]
                fid_arr = fid_arr[::step]
            self._anchors = arr
            self._anchor_fids = fid_arr
        else:
            self._anchors = np.zeros((0, 2))
            self._anchor_fids = np.zeros(0, dtype=np.int64)

    def feature_count(self):
        return len(self._features)

    def total_vertices(self):
        return self._total_vertices

    # ------------------------------------------------------------------
    # Settings changes
    # ------------------------------------------------------------------
    def set_transform(self, mode, lock):
        # Set mode and fixed-scale together, recompute only once.
        self.mode = mode
        self.lock_scale = lock
        self.recompute()

    def transform_label(self):
        '''Short label of the transform type, used e.g. in the output layer name.'''
        if self.mode == 'affine':
            return 'Affine'
        return 'Helmert(x1)' if self.lock_scale else 'Helmert'

    def set_quality(self, quality):
        if quality in QUALITY:
            self.quality = quality

    # ------------------------------------------------------------------
    # GCP operations
    # ------------------------------------------------------------------
    def add_gcp(self, src, dst):
        # In single-feature mode, confirm the target feature from the first source.
        self._ensure_single_locked(src)
        self.gcps.append(Gcp(src, dst))
        # On the first point, hide the source layer and switch to the preview.
        if len(self.gcps) == 1:
            self._hide_source_layer()
        self.recompute()

    def _ensure_single_locked(self, src):
        '''In single-feature mode, confirm the target feature if not yet set.

        If a node was grabbed, use its feature; for a free point / other layer,
        use the candidate feature nearest to src.
        '''
        if self.scope != 'single' or self._locked_fid is not None:
            return
        if self._last_snap_fid is not None:
            self._lock_single(self._last_snap_fid)
        elif self._candidates:
            fid = self._nearest_candidate_fid(QgsPointXY(src[0], src[1]))
            if fid is not None:
                self._lock_single(fid)

    def _lock_single(self, fid):
        '''Narrow single-feature mode to the target feature fid (only one
        feature moves in the preview).'''
        geom = dict(self._candidates).get(fid)
        if geom is None:
            return
        self._locked_fid = fid
        self._features = [(fid, geom)]
        self._rebuild_anchors()
        self._refresh_static()

    def _unlock_single(self):
        '''Release the single-feature narrowing and return to all candidates.'''
        self._locked_fid = None
        self._features = list(self._candidates)
        self._rebuild_anchors()
        self._refresh_static()

    def _refresh_static(self):
        '''Show the non-target features statically at their original position
        (only while the source layer is hidden).

        While the source layer is visible the features themselves are shown, so
        the static preview is skipped (avoids double drawing).
        '''
        self.rb_static.reset(self._geom_type)
        if (self.scope == 'single' and self._locked_fid is not None
                and self._layer_hidden):
            for f2, g2 in self._candidates:
                if f2 != self._locked_fid:
                    self.rb_static.addGeometry(g2, self.layer)

    def toggle_gcp(self, idx):
        if 0 <= idx < len(self.gcps):
            self.gcps[idx].active = not self.gcps[idx].active
            self.recompute()

    def set_active(self, idx, active):
        if 0 <= idx < len(self.gcps):
            self.gcps[idx].active = active
            self.recompute()

    def move_gcp_dst(self, idx, new_dst):
        '''Move and commit the destination (dst) coordinate of an existing GCP.'''
        if 0 <= idx < len(self.gcps):
            self.gcps[idx].dst = (float(new_dst[0]), float(new_dst[1]))
            self.recompute()

    def remove_last(self):
        if self.gcps:
            self.gcps.pop()
            if not self.gcps:
                self._restore_source_layer()
                # Single mode: go back to unconfirmed so another feature can be picked.
                if self.scope == 'single':
                    self._unlock_single()
            self.recompute()

    def clear_gcps(self):
        self.gcps = []
        self._restore_source_layer()
        if self.scope == 'single':
            self._unlock_single()
        self.recompute()

    def active_gcps(self):
        return [g for g in self.gcps if g.active]

    # ------------------------------------------------------------------
    # Transform computation and preview
    # ------------------------------------------------------------------
    def recompute(self):
        active = self.active_gcps()
        if active:
            src = np.array([g.src for g in active])
            dst = np.array([g.dst for g in active])
            self.matrix = transform.compute_matrix(
                src, dst, self.mode, self.lock_scale)
        else:
            self.matrix = transform._identity()

        self._draw_matrix = self.matrix
        self._request_preview()
        self._update_markers()
        self._notify_stats()

    def set_drag_preview(self, src, dst):
        '''Update only the preview with a transform that includes the
        provisional point (src -> dst) being dragged.'''
        # In single mode, confirm the target on drag start so only one feature moves.
        self._ensure_single_locked(src)
        active = self.active_gcps()
        srcs = [g.src for g in active] + [tuple(src)]
        dsts = [g.dst for g in active] + [tuple(dst)]
        self._dragging = True
        self._draw_matrix = transform.compute_matrix(
            np.array(srcs), np.array(dsts), self.mode, self.lock_scale)
        self._request_preview()

    def set_drag_gcp_preview(self, idx, new_dst):
        '''Update only the preview with an existing GCP's dst replaced by the
        dragged position.'''
        srcs = []
        dsts = []
        for j, g in enumerate(self.gcps):
            if not g.active:
                continue
            srcs.append(g.src)
            dsts.append(tuple(new_dst) if j == idx else g.dst)
        self._dragging = True
        if srcs:
            self._draw_matrix = transform.compute_matrix(
                np.array(srcs), np.array(dsts), self.mode, self.lock_scale)
        else:
            self._draw_matrix = self.matrix
        self._request_preview()

    def clear_drag_preview(self):
        '''Return to the normal preview when a drag is committed/cancelled.'''
        self._dragging = False
        self._draw_matrix = self.matrix
        self._request_preview()

    def preview_pos(self, src):
        '''Return the current preview position of the source coordinate src.'''
        p = transform.apply_matrix(self.matrix, [src])[0]
        return QgsPointXY(p[0], p[1])

    def _request_preview(self):
        '''Request a preview redraw with throttling.'''
        interval = QUALITY[self.quality]['interval'] / 1000.0
        now = time.monotonic()
        if now - self._last_update >= interval:
            self._do_preview()
        elif not self._timer.isActive():
            remaining = int((interval - (now - self._last_update)) * 1000)
            self._timer.start(max(1, remaining))

    def _do_preview(self):
        self._last_update = time.monotonic()
        self.rb_preview.reset(self._geom_type)
        t = transform.to_qtransform(self._draw_matrix)
        for _fid, g in self._features:
            gg = QgsGeometry(g)
            gg.transform(t)
            self.rb_preview.addGeometry(self._maybe_simplify(gg), self.layer)
        self.canvas.refresh()

    def _maybe_simplify(self, geom):
        '''Simplify the geometry for display only when it has many vertices.

        While dragging, lower the threshold and raise the tolerance to make it
        even lighter.
        '''
        threshold = SIMPLIFY_THRESHOLD
        factor = QUALITY[self.quality]['simplify']
        if self._dragging:
            threshold = SIMPLIFY_THRESHOLD // 4
            factor *= 2.0
        if self._total_vertices <= threshold:
            return geom
        tol = self.canvas.mapUnitsPerPixel() * factor
        simplified = geom.simplify(tol)
        return simplified if simplified and not simplified.isEmpty() else geom

    # ------------------------------------------------------------------
    # GCP markers (colored by error, active state shown)
    # ------------------------------------------------------------------
    def _clear_markers(self):
        for m in self.markers:
            self.canvas.scene().removeItem(m)
        self.markers = []

    def _update_markers(self):
        self._clear_markers()
        self.rb_residual.reset(Qgis.GeometryType.Line)

        stats = self._stats()
        per_point = stats['per_point']

        # Map active GCP indices (per_point covers active points only).
        active_idx = [i for i, g in enumerate(self.gcps) if g.active]
        mag_by_gcp = {}
        for k, gi in enumerate(active_idx):
            mag_by_gcp[gi] = per_point[k, 2] if k < len(per_point) else 0.0
        max_mag = float(per_point[:, 2].max()) if len(per_point) else 0.0

        for i, g in enumerate(self.gcps):
            m = QgsVertexMarker(self.canvas)
            m.setCenter(QgsPointXY(g.dst[0], g.dst[1]))
            m.setIconSize(14)
            m.setPenWidth(3)
            if g.active:
                m.setIconType(QgsVertexMarker.ICON_CROSS)
                m.setColor(residual_color(mag_by_gcp.get(i, 0.0), max_mag))
            else:
                m.setIconType(QgsVertexMarker.ICON_X)
                m.setColor(QColor(150, 150, 150))
            self.markers.append(m)

        # Residual display: for each GCP draw an independent segment from the
        # node that landed on the preview to the target point (dst). addGeometry
        # keeps each segment separate.
        for g in self.gcps:
            if not g.active:
                continue
            pred = transform.apply_matrix(self.matrix, [g.src])[0]
            seg = QgsGeometry.fromPolylineXY([
                QgsPointXY(pred[0], pred[1]),
                QgsPointXY(g.dst[0], g.dst[1]),
            ])
            self.rb_residual.addGeometry(seg, None)

    # ------------------------------------------------------------------
    # Residual statistics
    # ------------------------------------------------------------------
    def _stats(self):
        active = self.active_gcps()
        if not active:
            return {'per_point': np.zeros((0, 3)), 'rms': 0.0, 'std': 0.0}
        src = np.array([g.src for g in active])
        dst = np.array([g.dst for g in active])
        return transform.error_stats(self.matrix, src, dst)

    def scale_factors(self):
        '''Return the scale of the current transform matrix (overall, x, y).

        For Helmert (similarity) the three agree; affine can differ per axis.
        overall is the square root of the area ratio (= linear scale).
        '''
        a, b = self.matrix[0, 0], self.matrix[0, 1]
        d, e = self.matrix[1, 0], self.matrix[1, 1]
        det = a * e - b * d
        overall = float(np.sqrt(abs(det)))
        sx = float(np.hypot(a, d))
        sy = float(np.hypot(b, e))
        return overall, sx, sy

    def _notify_stats(self):
        if not self.on_update:
            return
        stats = self._stats()  # RMS/std computed from active points only
        rows = []
        mags = []
        for i, g in enumerate(self.gcps):
            # Show the residual at the current matrix for every point, inactive included.
            res = transform.residuals(self.matrix, [g.src], [g.dst])[0]
            ex, ey = float(res[0]), float(res[1])
            mag = float((ex * ex + ey * ey) ** 0.5)
            mags.append(mag)
            rows.append({
                'index': i,
                'active': g.active,
                'ex': ex, 'ey': ey, 'mag': mag,
            })
        overall, sx, sy = self.scale_factors()
        max_mag = max(mags) if mags else 0.0
        self.on_update({
            'rms': stats['rms'], 'std': stats['std'],
            'scale': overall, 'scale_x': sx, 'scale_y': sy,
            'max_mag': max_mag, 'rows': rows,
        })

    # ------------------------------------------------------------------
    # Snapping (queries from canvas interaction)
    # ------------------------------------------------------------------
    def snap_source(self, map_point, tol_map):
        '''Find the anchor vertex closest to map_point on the current preview.

        If found, return its source (original) coordinate, not the preview
        position. This supports taking nodes from the preview from the 2nd
        point onward.
        '''
        if len(self._anchors) == 0:
            return None
        prev = transform.apply_matrix(self.matrix, self._anchors)
        dx = prev[:, 0] - map_point.x()
        dy = prev[:, 1] - map_point.y()
        d2 = dx * dx + dy * dy
        i = int(np.argmin(d2))
        if np.sqrt(d2[i]) <= tol_map:
            # Remember the feature of the grabbed node (used to confirm the single target).
            if i < len(self._anchor_fids):
                self._last_snap_fid = int(self._anchor_fids[i])
            return (self._anchors[i, 0], self._anchors[i, 1])
        return None

    def _nearest_layer_vertex(self, map_point, tol_map, include_self):
        '''Return the vertex (map coordinate) closest to map_point among the
        visible vector layers.

        include_self=True also includes the target layer itself (self-snap).
        Only layers with the same CRS are considered; a rect filter scans only
        nearby features. None if nothing is found.
        '''
        rect = QgsRectangle(
            map_point.x() - tol_map, map_point.y() - tol_map,
            map_point.x() + tol_map, map_point.y() + tol_map)
        layers = list(self.canvas.layers())
        if include_self and self.layer not in layers:
            layers.append(self.layer)

        best = None
        best_d = tol_map
        seen = set()
        for lyr in layers:
            if not isinstance(lyr, QgsVectorLayer) or lyr.id() in seen:
                continue
            seen.add(lyr.id())
            if not include_self and lyr.id() == self.layer.id():
                continue
            if lyr.crs() != self.layer.crs():
                continue
            req = QgsFeatureRequest().setFilterRect(rect).setNoAttributes()
            for f in lyr.getFeatures(req):
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                for v in g.vertices():
                    d = ((v.x() - map_point.x()) ** 2 +
                         (v.y() - map_point.y()) ** 2) ** 0.5
                    if d <= best_d:
                        best_d = d
                        best = (v.x(), v.y())
        return best

    def snap_dest(self, map_point, tol_map):
        '''Destination snap: nearest vertex of any visible vector layer (incl. self).'''
        return self._nearest_layer_vertex(map_point, tol_map, include_self=True)

    def snap_source_other(self, map_point, tol_map):
        '''Source snap (other than the target geometry): nearest vertex of other visible layers.'''
        return self._nearest_layer_vertex(map_point, tol_map, include_self=False)

    def map_to_source(self, pt):
        '''Inverse-transform a map coordinate (current preview space) to source space.

        Used to bring a free point or another layer's node back into the source
        coordinate system when used as a transform source.
        '''
        A = self.matrix[:, :2]
        t = self.matrix[:, 2]
        try:
            a_inv = np.linalg.inv(A)
        except np.linalg.LinAlgError:
            return (float(pt[0]), float(pt[1]))
        o = a_inv @ (np.array([pt[0], pt[1]], dtype=float) - t)
        return (float(o[0]), float(o[1]))

    def nearest_gcp(self, map_point, tol_map):
        '''Return the index of the existing GCP (dst marker) near map_point.'''
        best = None
        best_d = tol_map
        for i, g in enumerate(self.gcps):
            d = ((g.dst[0] - map_point.x()) ** 2 +
                 (g.dst[1] - map_point.y()) ** 2) ** 0.5
            if d <= best_d:
                best_d = d
                best = i
        return best

    # ------------------------------------------------------------------
    # Source layer visibility control
    # ------------------------------------------------------------------
    def _hide_source_layer(self):
        node = QgsProject.instance().layerTreeRoot().findLayer(self.layer.id())
        if node:
            self._layer_was_visible = node.itemVisibilityChecked()
            node.setItemVisibilityChecked(False)
        self._layer_hidden = True
        self._refresh_static()

    def _restore_source_layer(self):
        node = QgsProject.instance().layerTreeRoot().findLayer(self.layer.id())
        if node:
            node.setItemVisibilityChecked(self._layer_was_visible)
        self._layer_hidden = False
        self._refresh_static()

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    def apply(self):
        '''Apply the final transform to all target features (full resolution)
        and return a new memory layer.'''
        if not self.active_gcps():
            return None
        t = transform.to_qtransform(self.matrix)

        geom_str = QgsWkbTypes.displayString(self.layer.wkbType())
        crs = self.layer.crs().authid()
        uri = '{}?crs={}'.format(geom_str, crs)
        out_name = '{}_{}'.format(self.layer.name(), self.transform_label())
        out = QgsVectorLayer(uri, out_name, 'memory')
        dp = out.dataProvider()
        dp.addAttributes(self.layer.fields().toList())
        out.updateFields()

        in_scope = set(fid for fid, _ in self._features)
        new_feats = []
        for f in self.layer.getFeatures():
            if f.id() not in in_scope:
                continue
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            gg = QgsGeometry(g)
            gg.transform(t)
            nf = QgsFeature(out.fields())
            nf.setAttributes(f.attributes())
            nf.setGeometry(gg)
            new_feats.append(nf)
        dp.addFeatures(new_feats)
        out.updateExtents()

        # Keep GCP/error info in the layer custom properties.
        out.setCustomProperty('fvg/mode', self.mode)
        out.setCustomProperty('fvg/lock_scale', str(self.lock_scale))
        out.setCustomProperty('fvg/gcp_count', str(len(self.active_gcps())))
        stats = self._stats()
        out.setCustomProperty('fvg/rms', '{:.6f}'.format(stats['rms']))
        overall, sx, sy = self.scale_factors()
        out.setCustomProperty('fvg/scale', '{:.6f}'.format(overall))
        out.setCustomProperty('fvg/scale_x', '{:.6f}'.format(sx))
        out.setCustomProperty('fvg/scale_y', '{:.6f}'.format(sy))

        QgsProject.instance().addMapLayer(out)
        return out

    def apply_add(self):
        '''Add the transformed features to the current layer as new features
        (run in edit mode).

        Done with the layer in edit mode so it can be undone with Ctrl+Z.
        Returns the number of features added, or None on failure.
        '''
        if not self.active_gcps():
            return None
        if not self._ensure_editable():
            return None
        t = transform.to_qtransform(self.matrix)
        in_scope = set(fid for fid, _ in self._features)
        feats = []
        for f in self.layer.getFeatures():
            if f.id() not in in_scope:
                continue
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            gg = QgsGeometry(g)
            gg.transform(t)
            nf = QgsFeature(self.layer.fields())
            nf.setAttributes(f.attributes())
            nf.setGeometry(gg)
            feats.append(nf)
        self.layer.beginEditCommand('Freehand Vector Georeferencer: add')
        self.layer.addFeatures(feats)
        self.layer.endEditCommand()
        self.layer.triggerRepaint()
        return len(feats)

    def apply_edit(self):
        '''Replace the geometry of the current layer's target features with the
        transformed geometry (run in edit mode).

        Done with the layer in edit mode so it can be undone with Ctrl+Z.
        Returns the number of features changed, or None on failure.
        '''
        if not self.active_gcps():
            return None
        if not self._ensure_editable():
            return None
        t = transform.to_qtransform(self.matrix)
        self.layer.beginEditCommand('Freehand Vector Georeferencer: edit')
        n = 0
        for fid, g in self._features:
            gg = QgsGeometry(g)
            gg.transform(t)
            self.layer.changeGeometry(fid, gg)
            n += 1
        self.layer.endEditCommand()
        self.layer.triggerRepaint()
        return n

    def _ensure_editable(self):
        '''Put the layer into edit mode. No-op if already editing. Returns ok.'''
        if self.layer.isEditable():
            return True
        return bool(self.layer.startEditing())

    def save_gcps(self, path):
        '''Save the GCPs and residual info to CSV.'''
        stats = self._stats()
        per_point = stats['per_point']
        active_idx = [i for i, g in enumerate(self.gcps) if g.active]
        with open(path, 'w', newline='', encoding='utf-8') as fp:
            w = csv.writer(fp)
            w.writerow(['index', 'active', 'srcX', 'srcY', 'dstX', 'dstY',
                        'errX', 'errY', 'residual'])
            for i, g in enumerate(self.gcps):
                if g.active and i in active_idx:
                    k = active_idx.index(i)
                    ex, ey, mag = per_point[k] if k < len(per_point) else (0, 0, 0)
                else:
                    ex = ey = mag = ''
                w.writerow([i, int(g.active), g.src[0], g.src[1],
                            g.dst[0], g.dst[1], ex, ey, mag])
            overall, sx, sy = self.scale_factors()
            w.writerow([])
            w.writerow(['# mode', self.mode, 'lock_scale', int(self.lock_scale)])
            w.writerow(['# RMS', stats['rms'], 'STD', stats['std']])
            w.writerow(['# scale', overall, 'scaleX', sx, 'scaleY', sy])

    def load_gcps(self, path):
        '''Load and reproduce GCPs from CSV (to reuse the same transform on
        another layer).

        Reads the format written by save_gcps (index, active, srcX, srcY, dstX,
        dstY, ...). Assumes the same CRS and uses the src/dst coordinates as is.
        Returns the number of points loaded.
        '''
        loaded = []
        with open(path, newline='', encoding='utf-8') as fp:
            for row in csv.reader(fp):
                if not row or row[0].strip().startswith('#'):
                    continue
                if row[0].strip() == 'index':
                    continue
                try:
                    active = bool(int(row[1]))
                    sx, sy = float(row[2]), float(row[3])
                    dx, dy = float(row[4]), float(row[5])
                except (ValueError, IndexError):
                    continue
                loaded.append(((sx, sy), (dx, dy), active))
        if not loaded:
            return 0

        self.gcps = [Gcp(src, dst, active) for src, dst, active in loaded]
        # In single mode, if not yet confirmed, target the candidate nearest the first GCP.
        if (self.scope == 'single' and self._locked_fid is None
                and self._candidates):
            first = QgsPointXY(loaded[0][0][0], loaded[0][0][1])
            fid = self._nearest_candidate_fid(first)
            if fid is not None:
                self._lock_single(fid)
        if self.gcps:
            self._hide_source_layer()
        self.recompute()
        return len(self.gcps)

    def _nearest_candidate_fid(self, point):
        '''Return the candidate feature id nearest to point (single mode).'''
        pg = QgsGeometry.fromPointXY(point)
        best = None
        best_d = None
        for fid, g in self._candidates:
            d = g.distance(pg)
            if best_d is None or d < best_d:
                best_d = d
                best = fid
        return best

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------
    def cleanup(self):
        self._timer.stop()
        self._clear_markers()
        self.canvas.scene().removeItem(self.rb_preview)
        self.canvas.scene().removeItem(self.rb_static)
        self.canvas.scene().removeItem(self.rb_residual)
        self._restore_source_layer()
        self.canvas.refresh()
