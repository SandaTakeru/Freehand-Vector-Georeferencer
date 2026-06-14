# -*- coding: utf-8 -*-
'''ジオリファレンス作業のコアエンジン。

GCP（対応点）の保持・変換行列の計算・プレビュー描画・マーカー表示・
残差計算・出力レイヤ生成までを担当する。UI（ドック）からはこのクラスを操作する。
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


# スナップ用アンカー頂点の上限（大量頂点データ対策で間引く）
MAX_ANCHORS = 5000
# プレビュー簡略化を始める総頂点数のしきい値
SIMPLIFY_THRESHOLD = 20000

# プレビュー品質ごとの更新間隔（ミリ秒）と簡略化係数
QUALITY = {
    'High (30fps)': {'interval': 33, 'simplify': 0.5},
    'Medium (10fps)': {'interval': 100, 'simplify': 1.0},
    'Light (4fps)': {'interval': 250, 'simplify': 2.0},
    'Minimum (1fps)': {'interval': 1000, 'simplify': 3.0},
}
DEFAULT_QUALITY = 'Medium (10fps)'

# 残差カラーの端点（緑→黄→赤）
_COLOR_GREEN = (0, 180, 0)
_COLOR_YELLOW = (230, 200, 0)
_COLOR_RED = (220, 0, 0)


def _lerp(a, b, f):
    return int(round(a + (b - a) * f))


def residual_color(mag, max_mag):
    '''残差の大きさを 0..max_mag でリニアに 緑→黄→赤 へ補間した色を返す。

    同じ mag は同じ色になる。max_mag が 0（全点が同値・残差なし）なら緑。
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
    '''1 つの対応点。src は元座標、dst は合わせ込み先座標。'''

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
        self.on_update = on_update    # 統計・テーブル更新を UI へ通知するコールバック

        self.gcps = []
        self.matrix = transform._identity()
        self._draw_matrix = transform._identity()  # 実際に描画する行列（ドラッグ中は仮値）
        self._dragging = False        # ドラッグ中はプレビューを軽めにする

        self._geom_type = QgsWkbTypes.geometryType(layer.wkbType())
        self._features = []           # (fid, QgsGeometry) のリスト（変換対象の元ジオメトリ）
        self._anchors = np.zeros((0, 2))
        self._anchor_fids = np.zeros(0, dtype=np.int64)  # 各アンカー頂点の所属地物 id
        self._total_vertices = 0
        self._layer_was_visible = True
        self._layer_hidden = False

        # 単一地物モード用: 全地物を候補として保持し、最初に掴んだノードの地物に絞る
        self._candidates = []         # (fid, QgsGeometry) の全候補
        self._locked_fid = None       # 確定した対象地物 id（単一地物モード）
        self._last_snap_fid = None    # 直近に snap_source で掴んだノードの地物 id

        # プレビュー用ラバーバンド
        self.rb_preview = QgsRubberBand(self.canvas, self._geom_type)
        self.rb_preview.setColor(QColor(30, 120, 255, 180))
        self.rb_preview.setFillColor(QColor(30, 120, 255, 40))
        self.rb_preview.setWidth(2)
        # 単一地物モードで対象外の地物を元位置に表示しておく静的プレビュー
        self.rb_static = QgsRubberBand(self.canvas, self._geom_type)
        self.rb_static.setColor(QColor(120, 120, 120, 160))
        self.rb_static.setFillColor(QColor(120, 120, 120, 30))
        self.rb_static.setWidth(1)
        # 残差ベクトル（変換後src → dst）の表示
        self.rb_residual = QgsRubberBand(self.canvas, Qgis.GeometryType.Line)
        self.rb_residual.setColor(QColor(255, 0, 0, 200))
        self.rb_residual.setWidth(1)

        self.markers = []             # GCP ごとの QgsVertexMarker

        # プレビュー更新のスロットリング
        self._last_update = 0.0
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._do_preview)

        self._build_scope()

    # ------------------------------------------------------------------
    # 初期化・対象地物の収集
    # ------------------------------------------------------------------
    def _build_scope(self):
        '''スコープ設定に従って対象地物とスナップ用アンカーを集める。

        単一地物モードでは、開始時は全地物を候補として読み込み、最初に掴んだ
        ノードの地物を対象として確定する（_lock_single で絞り込む）。
        '''
        if self.scope == 'all':
            feats = list(self.layer.getFeatures())
        elif self.scope == 'selected':
            feats = list(self.layer.getSelectedFeatures())
        else:  # single: 全地物を候補にする
            feats = list(self.layer.getFeatures())

        collected = []
        for f in feats:
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            collected.append((f.id(), QgsGeometry(g)))

        if self.scope == 'single':
            # 候補として全地物を保持し、確定前は全地物をプレビュー対象にする
            self._candidates = collected
            self._features = list(collected)
        else:
            self._candidates = []
            self._features = collected

        self._rebuild_anchors()

    def _rebuild_anchors(self):
        '''現在の _features からスナップ用アンカー（頂点と所属地物 id）を作り直す。'''
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
            # 多すぎる場合はストライドで間引く（スナップ候補の軽量化）
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
    # 設定変更
    # ------------------------------------------------------------------
    def set_transform(self, mode, lock):
        # モードと等倍固定をまとめて設定し、再計算は一度だけ
        self.mode = mode
        self.lock_scale = lock
        self.recompute()

    def transform_label(self):
        '''出力レイヤ名などに使う変換種別の短いラベル。'''
        if self.mode == 'affine':
            return 'Affine'
        return 'Helmert(x1)' if self.lock_scale else 'Helmert'

    def set_quality(self, quality):
        if quality in QUALITY:
            self.quality = quality

    # ------------------------------------------------------------------
    # GCP 操作
    # ------------------------------------------------------------------
    def add_gcp(self, src, dst):
        # 単一地物モードでは、最初の変換元から対象地物を確定する
        self._ensure_single_locked(src)
        self.gcps.append(Gcp(src, dst))
        # 1 点目を追加したら元レイヤ表示を消し、プレビューに切り替える
        if len(self.gcps) == 1:
            self._hide_source_layer()
        self.recompute()

    def _ensure_single_locked(self, src):
        '''単一地物モードで未確定なら対象地物を確定する。

        ノードを掴んでいればその地物、任意点/他レイヤなら src に最も近い候補地物。
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
        '''単一地物モードで対象地物を fid に絞り込む（プレビューで動くのは1地物だけ）。'''
        geom = dict(self._candidates).get(fid)
        if geom is None:
            return
        self._locked_fid = fid
        self._features = [(fid, geom)]
        self._rebuild_anchors()
        self._refresh_static()

    def _unlock_single(self):
        '''単一地物モードの絞り込みを解除し、全候補に戻す。'''
        self._locked_fid = None
        self._features = list(self._candidates)
        self._rebuild_anchors()
        self._refresh_static()

    def _refresh_static(self):
        '''対象外の地物を元位置に静的表示する（元レイヤ非表示中のみ）。

        元レイヤ表示中は地物本体が見えているので静的表示はしない（二重描画回避）。
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
        '''既存 GCP の変換先（dst）座標を移動して確定する。'''
        if 0 <= idx < len(self.gcps):
            self.gcps[idx].dst = (float(new_dst[0]), float(new_dst[1]))
            self.recompute()

    def remove_last(self):
        if self.gcps:
            self.gcps.pop()
            if not self.gcps:
                self._restore_source_layer()
                # 単一地物モードは対象未確定に戻し、別地物を選び直せるようにする
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
    # 変換計算とプレビュー
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
        '''ドラッグ中の仮対応点(src→dst)を加えた変換でプレビューだけ更新する。'''
        # 単一地物モードはドラッグ開始時点で対象を1地物に確定し、動くのを1つに絞る
        self._ensure_single_locked(src)
        active = self.active_gcps()
        srcs = [g.src for g in active] + [tuple(src)]
        dsts = [g.dst for g in active] + [tuple(dst)]
        self._dragging = True
        self._draw_matrix = transform.compute_matrix(
            np.array(srcs), np.array(dsts), self.mode, self.lock_scale)
        self._request_preview()

    def set_drag_gcp_preview(self, idx, new_dst):
        '''既存 GCP の dst をドラッグ中の位置に置き換えてプレビューだけ更新する。'''
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
        '''ドラッグ確定/中断時に通常プレビューへ戻す。'''
        self._dragging = False
        self._draw_matrix = self.matrix
        self._request_preview()

    def preview_pos(self, src):
        '''元座標 src の現在のプレビュー上の位置を返す。'''
        p = transform.apply_matrix(self.matrix, [src])[0]
        return QgsPointXY(p[0], p[1])

    def _request_preview(self):
        '''プレビュー描画をスロットリングしながら要求する。'''
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
        '''頂点数が多い場合のみ、表示用にジオメトリを簡略化する。

        ドラッグ中はしきい値を下げ・許容量を増やしてさらに軽くする。
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
    # GCP マーカー（誤差で色分け・アクティブ状態表示）
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

        # アクティブ GCP のインデックス対応（per_point はアクティブ分のみ）
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

        # 残差表示: GCP ごとに「プレビュー上に着地したノード」→「目標点(dst)」を
        # 結ぶ独立した線分として描く（addGeometry で各線分を分離して描画）。
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
    # 残差統計
    # ------------------------------------------------------------------
    def _stats(self):
        active = self.active_gcps()
        if not active:
            return {'per_point': np.zeros((0, 3)), 'rms': 0.0, 'std': 0.0}
        src = np.array([g.src for g in active])
        dst = np.array([g.dst for g in active])
        return transform.error_stats(self.matrix, src, dst)

    def scale_factors(self):
        '''現在の変換行列の拡大率を返す (overall, x方向, y方向)。

        ヘルメルト（相似）なら3つは一致する。アフィンは方向ごとに異なりうる。
        overall は面積比の平方根（= 線形拡大率）。
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
        stats = self._stats()  # RMS/標準偏差はアクティブ点のみで算出
        rows = []
        mags = []
        for i, g in enumerate(self.gcps):
            # 無効な GCP も含め、全点について現在の行列での残差を表示する
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
    # スナップ（キャンバス操作からの問い合わせ）
    # ------------------------------------------------------------------
    def snap_source(self, map_point, tol_map):
        '''現在のプレビュー上で map_point に最も近いアンカー頂点を探す。

        見つかれば、その頂点の「元座標」を返す（プレビュー位置ではなく元位置）。
        2 点目以降はプレビュー上のノードから取得する仕様に対応する。
        '''
        if len(self._anchors) == 0:
            return None
        prev = transform.apply_matrix(self.matrix, self._anchors)
        dx = prev[:, 0] - map_point.x()
        dy = prev[:, 1] - map_point.y()
        d2 = dx * dx + dy * dy
        i = int(np.argmin(d2))
        if np.sqrt(d2[i]) <= tol_map:
            # 掴んだノードの所属地物を控える（単一地物モードの対象確定に使う）
            if i < len(self._anchor_fids):
                self._last_snap_fid = int(self._anchor_fids[i])
            return (self._anchors[i, 0], self._anchors[i, 1])
        return None

    def _nearest_layer_vertex(self, map_point, tol_map, include_self):
        '''表示中ベクタレイヤから map_point 最近傍の頂点（マップ座標）を返す。

        include_self=True なら対象レイヤ自身も含む（自己スナップ）。
        同一 CRS のレイヤのみ対象。範囲フィルタで近傍地物だけ走査。なければ None。
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
        '''変換先スナップ: 表示中の全ベクタレイヤ（自己含む）の最近傍頂点。'''
        return self._nearest_layer_vertex(map_point, tol_map, include_self=True)

    def snap_source_other(self, map_point, tol_map):
        '''変換元スナップ（対象ジオメトリ以外）: 他の表示中レイヤの最近傍頂点。'''
        return self._nearest_layer_vertex(map_point, tol_map, include_self=False)

    def map_to_source(self, pt):
        '''マップ座標（現在のプレビュー空間）を元座標へ逆変換する。

        任意点や他レイヤのノードを変換元に使うとき、元座標系へ戻すために使う。
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
        '''map_point に近い既存 GCP（dst マーカー）のインデックスを返す。'''
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
    # 元レイヤ表示の制御
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
    # 出力
    # ------------------------------------------------------------------
    def apply(self):
        '''最終変換を全対象地物（フル解像度）に適用し、新規メモリレイヤを返す。'''
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

        # GCP・誤差情報をレイヤのカスタムプロパティに残す
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
        '''変換後の地物を現在のレイヤへ新規地物として追加する（編集モードで実行）。

        レイヤを編集モードにしたまま追加するので、Ctrl+Z で取り消せる。
        追加した地物数を返す。失敗時は None。
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
        '''現在のレイヤの対象地物のジオメトリを変換後に置き換える（編集モードで実行）。

        レイヤを編集モードにしたまま変更するので、Ctrl+Z で取り消せる。
        変更した地物数を返す。失敗時は None。
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
        '''レイヤを編集モードにする。既に編集中なら何もしない。可否を返す。'''
        if self.layer.isEditable():
            return True
        return bool(self.layer.startEditing())

    def save_gcps(self, path):
        '''GCP と残差情報を CSV に保存する。'''
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
        '''CSV から GCP を読み込んで再現する（別レイヤへ同じ変換を流用する用途）。

        save_gcps が出力した形式（index, active, srcX, srcY, dstX, dstY, ...）を読む。
        同一参照系前提で src/dst の座標をそのまま使う。読み込んだ点数を返す。
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
        # 単一地物モードで未確定なら、先頭 GCP に最も近い候補地物を対象にする
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
        '''単一地物モードの候補のうち、point に最も近い地物 id を返す。'''
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
    # 後始末
    # ------------------------------------------------------------------
    def cleanup(self):
        self._timer.stop()
        self._clear_markers()
        self.canvas.scene().removeItem(self.rb_preview)
        self.canvas.scene().removeItem(self.rb_static)
        self.canvas.scene().removeItem(self.rb_residual)
        self._restore_source_layer()
        self.canvas.refresh()
