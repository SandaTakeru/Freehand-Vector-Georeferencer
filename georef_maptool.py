# -*- coding: utf-8 -*-
'''ジオリファレンス用のマップツール。

新規 GCP の追加:
  1. 変換元を mouse press で選ぶ（優先順: 対象ジオメトリのノード →
     他の表示中レイヤのノード → 任意点）
  2. 対応する新座標位置で mouse release → GCP を追加

既存 GCP の操作:
  - 軽くクリック → 変換計算への有効/無効を切替
  - ドラッグ → 変換先(dst)の位置を移動

補助:
  - マウス下で掴める元ノードを常時ハイライト表示
  - ドラッグ中はカーソル位置を仮対応点としてライブプレビュー
  - 変換先は表示中の全ベクタレイヤ（自己含む）の頂点へ吸着
'''

from qgis.core import Qgis, QgsPointXY
from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtGui import QColor

# スナップ・クリック判定のピクセル許容量
SNAP_PIXELS = 14
CLICK_PIXELS = 6


class GeorefMapTool(QgsMapTool):
    def __init__(self, iface, session):
        self.iface = iface
        self.canvas = iface.mapCanvas()
        super().__init__(self.canvas)
        self.session = session

        self._pending_src = None      # スナップした元座標 (x, y)
        self._press_screen = None     # 押下時のスクリーン座標
        self._press_gcp = None        # 押下時に近接していた既存 GCP の index

        # ドラッグ中の変位ガイド線
        self.rb_drag = QgsRubberBand(self.canvas, Qgis.GeometryType.Line)
        self.rb_drag.setColor(QColor(255, 140, 0, 220))
        self.rb_drag.setWidth(2)

        # 掴める元ノードのハイライト（黄色の丸）
        self.hoverMarker = QgsVertexMarker(self.canvas)
        self.hoverMarker.setIconType(QgsVertexMarker.ICON_CIRCLE)
        self.hoverMarker.setColor(QColor(255, 210, 0))
        self.hoverMarker.setIconSize(16)
        self.hoverMarker.setPenWidth(3)
        self.hoverMarker.hide()

        # 変換先スナップのハイライト（水色の箱）
        self.destMarker = QgsVertexMarker(self.canvas)
        self.destMarker.setIconType(QgsVertexMarker.ICON_BOX)
        self.destMarker.setColor(QColor(0, 200, 255))
        self.destMarker.setIconSize(16)
        self.destMarker.setPenWidth(3)
        self.destMarker.hide()

    def _tol_map(self, pixels):
        return self.canvas.mapUnitsPerPixel() * pixels

    def _snapped_dest(self, screen_pos):
        '''スクリーン座標から変換先座標を求める（可能ならスナップ）。

        戻り値: (QgsPointXY, snapped: bool)
        '''
        cur = self.toMapCoordinates(screen_pos)
        snap = self.session.snap_dest(cur, self._tol_map(SNAP_PIXELS))
        if snap is not None:
            return QgsPointXY(snap[0], snap[1]), True
        return cur, False

    def canvasPressEvent(self, e):
        if not self.session:
            return
        self._press_screen = e.pos()
        map_pt = self.toMapCoordinates(e.pos())

        # まず既存 GCP の近接判定。近ければ「既存 GCP の編集」を優先する
        # （クリック＝有効切替 / ドラッグ＝変換先 dst の移動）。
        self._press_gcp = self.session.nearest_gcp(
            map_pt, self._tol_map(SNAP_PIXELS))
        if self._press_gcp is not None:
            self._pending_src = None
            return

        # 変換元の決定。優先順:
        #   1. 対象ジオメトリのノード（厳密な元座標）
        #   2. 他の表示中レイヤのノード（マップ座標→逆変換で元座標へ）
        #   3. 任意点（ノードなし。マップ座標→逆変換で元座標へ）
        tol = self._tol_map(SNAP_PIXELS)
        src = self.session.snap_source(map_pt, tol)
        if src is not None:
            self._pending_src = src
            self.hoverMarker.setCenter(self.session.preview_pos(src))
            self.hoverMarker.show()
        else:
            snapped = self.session.snap_source_other(map_pt, tol)
            pick = snapped if snapped is not None else (map_pt.x(), map_pt.y())
            # 任意点/他レイヤは対象地物を特定しない（単一地物は近傍地物で確定）
            self.session._last_snap_fid = None
            self._pending_src = self.session.map_to_source(pick)
            self.hoverMarker.setCenter(QgsPointXY(pick[0], pick[1]))
            self.hoverMarker.show()

    def canvasMoveEvent(self, e):
        if not self.session:
            return

        if self._press_gcp is not None:
            # 既存 GCP の変換先 dst をドラッグ移動（ライブプレビュー）
            dest, snapped = self._snapped_dest(e.pos())
            start = self.session.preview_pos(self.session.gcps[self._press_gcp].src)
            self.rb_drag.reset(Qgis.GeometryType.Line)
            self.rb_drag.addPoint(start, False)
            self.rb_drag.addPoint(dest, True)
            if snapped:
                self.destMarker.setCenter(dest)
                self.destMarker.show()
            else:
                self.destMarker.hide()
            self.session.set_drag_gcp_preview(
                self._press_gcp, (dest.x(), dest.y()))
        elif self._pending_src is not None:
            # ドラッグ中: カーソル位置を仮対応点としてライブプレビュー
            dest, snapped = self._snapped_dest(e.pos())
            start = self.session.preview_pos(self._pending_src)
            self.rb_drag.reset(Qgis.GeometryType.Line)
            self.rb_drag.addPoint(start, False)
            self.rb_drag.addPoint(dest, True)
            if snapped:
                self.destMarker.setCenter(dest)
                self.destMarker.show()
            else:
                self.destMarker.hide()
            self.session.set_drag_preview(
                self._pending_src, (dest.x(), dest.y()))
        else:
            # ホバー中: 掴める元ノード（対象ジオメトリ／他レイヤ）をハイライト
            map_pt = self.toMapCoordinates(e.pos())
            tol = self._tol_map(SNAP_PIXELS)
            src = self.session.snap_source(map_pt, tol)
            if src is not None:
                self.hoverMarker.setCenter(self.session.preview_pos(src))
                self.hoverMarker.show()
            else:
                other = self.session.snap_source_other(map_pt, tol)
                if other is not None:
                    self.hoverMarker.setCenter(QgsPointXY(other[0], other[1]))
                    self.hoverMarker.show()
                else:
                    self.hoverMarker.hide()

    def canvasReleaseEvent(self, e):
        self.rb_drag.reset(Qgis.GeometryType.Line)
        self.destMarker.hide()
        if self.session:
            self.session.clear_drag_preview()
        if not self.session or self._press_screen is None:
            self._reset_pending()
            return

        moved = (e.pos() - self._press_screen).manhattanLength()

        # 既存 GCP 上の操作: クリック＝有効切替 / ドラッグ＝変換先 dst の移動
        if self._press_gcp is not None:
            if moved <= CLICK_PIXELS:
                self.session.toggle_gcp(self._press_gcp)
            else:
                dest, _snapped = self._snapped_dest(e.pos())
                self.session.move_gcp_dst(
                    self._press_gcp, (dest.x(), dest.y()))
            self._reset_pending()
            return

        # 変換元が決まっていれば GCP を追加（押下時に必ず決まる）
        if self._pending_src is not None:
            dest, _snapped = self._snapped_dest(e.pos())
            self.session.add_gcp(self._pending_src, (dest.x(), dest.y()))
        self._reset_pending()

    def _reset_pending(self):
        self._pending_src = None
        self._press_screen = None
        self._press_gcp = None
        self.hoverMarker.hide()

    def deactivate(self):
        self.rb_drag.reset(Qgis.GeometryType.Line)
        self.hoverMarker.hide()
        self.destMarker.hide()
        super().deactivate()

    def cleanup(self):
        self.canvas.scene().removeItem(self.rb_drag)
        self.canvas.scene().removeItem(self.hoverMarker)
        self.canvas.scene().removeItem(self.destMarker)
