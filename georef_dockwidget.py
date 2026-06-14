# -*- coding: utf-8 -*-
'''操作パネル（ドックウィジェット）。

レイヤ・対象範囲・変換モードの選択、GCP テーブルと誤差表示、適用/保存を提供する。
数値入力 UI は持たない（ユーザーはパラメータを直接入力しない）。
'''

import os
import tempfile
import time

from qgis.core import (
    Qgis, QgsMapLayerProxyModel, QgsMessageLog, QgsProject, QgsVectorLayer,
)
from qgis.gui import QgsDockWidget, QgsMapLayerComboBox, QgsMessageViewer
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .georef_session import (
    DEFAULT_QUALITY, QUALITY, GeorefSession, residual_color,
)
from .georef_maptool import GeorefMapTool


class _NumItem(QTableWidgetItem):
    '''数値順にソートするテーブル項目（表示は整形済み文字列）。空値は末尾。'''

    def __init__(self, text, value):
        super().__init__(text)
        self._value = value
        # 編集不可・選択と表示のみ
        self.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)

    def __lt__(self, other):
        a = self._value if self._value is not None else float('inf')
        b = getattr(other, '_value', None)
        b = b if b is not None else float('inf')
        return a < b


class _CheckItem(QTableWidgetItem):
    '''チェック状態（有効を先）→ GCP 番号 の順でソートするテーブル項目。'''

    def __lt__(self, other):
        sa = 0 if self.checkState() == Qt.CheckState.Checked else 1
        sb = 0 if other.checkState() == Qt.CheckState.Checked else 1
        if sa != sb:
            return sa < sb
        ia = self.data(Qt.ItemDataRole.UserRole) or 0
        ib = other.data(Qt.ItemDataRole.UserRole) or 0
        return ia < ib


class GeorefDockWidget(QgsDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__('Freehand Vector Georeferencer', parent)
        self.iface = iface
        self.session = None
        # 停止後も Save できるように、直近の（停止済み）セッションを保持する
        self._last_session = None
        self.maptool = None
        self._syncing = False
        # 選択数の変化を監視している対象レイヤ（開始可否の判定に使う）
        self._sel_layer = None

        self._build_ui()
        self._preselect_active_layer()

    def _preselect_active_layer(self):
        # アクティブレイヤがベクタなら対象レイヤの既定にする。
        # セッション実行中は座標基準が変わるため上書きしない。
        if self.session is not None:
            return
        layer = self.iface.activeLayer()
        if layer is not None and isinstance(layer, QgsVectorLayer):
            self.layerCombo.setLayer(layer)
        self._on_layer_changed()

    def _on_layer_changed(self, layer=None):
        # 対象レイヤが変わったら CRS 警告・選択監視・開始可否を更新する
        self._update_crs_warning()
        self._reconnect_selection()
        self._update_idle_primary()

    def _reconnect_selection(self):
        # 選択数の変化で開始可否が変わるため、対象レイヤの selectionChanged を監視
        if self._sel_layer is not None:
            try:
                self._sel_layer.selectionChanged.disconnect(
                    self._on_selection_changed)
            except (TypeError, RuntimeError):
                pass
            self._sel_layer = None
        layer = self.layerCombo.currentLayer()
        if isinstance(layer, QgsVectorLayer):
            layer.selectionChanged.connect(self._on_selection_changed)
            self._sel_layer = layer

    def _on_selection_changed(self, *args):
        self._update_idle_primary()

    def _can_start(self):
        # 開始しても意味があるか（地物がある／選択スコープなら選択がある）
        layer = self.layerCombo.currentLayer()
        if not isinstance(layer, QgsVectorLayer):
            return False
        if layer.featureCount() == 0:
            return False
        if (self._current_scope() == 'selected'
                and layer.selectedFeatureCount() == 0):
            return False
        return True

    def _update_idle_primary(self, *args):
        # 待機中（セッション未開始）の Start は、開始可能なときだけ押せる
        if self.session is None:
            self.primaryBtn.setEnabled(self._can_start())

    def _update_crs_warning(self):
        # レイヤCRSとプロジェクトCRSが異なれば、赤文字の警告を常時表示する
        layer = self.layerCombo.currentLayer()
        proj_crs = QgsProject.instance().crs()
        if layer is not None and layer.crs() != proj_crs:
            self.crsWarn.setText(
                '<a href="#" style="color:#cc0000; text-decoration:none;">'
                '⚠ Layer CRS ({}) differs from project CRS ({}). '
                'Match them. Click for details.</a>'.format(
                    layer.crs().authid() or 'unknown',
                    proj_crs.authid() or 'unknown'))
            self.crsWarn.show()
        else:
            self.crsWarn.hide()

    def _show_crs_detail(self, _link):
        # クリックで詳細説明を標準のメッセージビューア（読み取り専用）に表示
        viewer = QgsMessageViewer(self)
        viewer.setWindowTitle('CRS / projection — please read')
        viewer.setMessageAsHtml(self._crs_detail_html())
        viewer.exec()

    def _crs_detail_html(self):
        return (
            '<h3>Keep the layer CRS and the project CRS the same.</h3>'
            '<p>This is the one rule to follow. Set the project CRS equal to '
            'the target layer’s CRS before georeferencing.</p>'
            '<hr>'
            '<p><b>Why</b></p>'
            '<ul>'
            '<li>This plugin applies a plain 2D affine transform '
            '(Helmert / Affine). It does <b>not</b> reproject and does '
            '<b>not</b> apply any geodetic or scale-factor correction.</li>'
            '<li>The transform itself never bends shapes: affine maps '
            'straight lines to straight lines, and Helmert keeps the shape.</li>'
            '<li>GCP <i>source</i> coordinates are read in the layer CRS, while '
            '<i>destination</i> points are read in the project/map CRS. If the '
            'two CRS differ, these are mixed and the result becomes wrong, and '
            'the live preview is implicitly reprojected (layer → map), '
            'which can look distorted.</li>'
            '<li>Plane rectangular CS scale factor: coordinates are treated as '
            'a flat plane; the scale factor that varies across a zone is not '
            'corrected. Congruent planar figures stay congruent; true ground '
            'distances are not adjusted.</li>'
            '</ul>'
            '<p><b>Bottom line:</b> make the project CRS match the layer CRS, '
            'then georeference.</p>')

    # ------------------------------------------------------------------
    # UI 構築
    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)

        # 設定フォーム（上から Transform → Scope → Target layer）
        form = QFormLayout()
        # 変換モード（プルダウン）。currentData は (mode, lock_scale) のタプル
        self.transformCombo = QComboBox()
        self.transformCombo.addItem(
            'Helmert (move / rotate, fixed scale)', ('helmert', True))
        self.transformCombo.addItem(
            'Helmert (move / rotate / scale)', ('helmert', False))
        self.transformCombo.addItem(
            'Affine (move / rotate / scale / shear)', ('affine', False))
        self.transformCombo.setCurrentIndex(0)  # 既定: ヘルメルト（等倍固定）
        form.addRow('Transform', self.transformCombo)

        # 対象範囲（プルダウン）。内部値は currentData で取得する
        self.scopeCombo = QComboBox()
        self.scopeCombo.addItem('Single feature', 'single')
        self.scopeCombo.addItem('Selected features only', 'selected')
        self.scopeCombo.addItem('Whole layer', 'all')
        self.scopeCombo.setCurrentIndex(0)  # 既定は単一地物
        form.addRow('Scope', self.scopeCombo)

        self.layerCombo = QgsMapLayerComboBox()
        self.layerCombo.setFilters(QgsMapLayerProxyModel.Filter.VectorLayer)
        form.addRow('Target layer', self.layerCombo)

        # 適用方法を選ぶプルダウン（同じフォームに入れて他と幅を揃える）
        self.applyModeCombo = QComboBox()
        self.applyModeCombo.addItem('Generate new layer', 'new')
        self.applyModeCombo.addItem('Add to current layer', 'add')
        self.applyModeCombo.addItem('Edit current layer', 'edit')
        form.addRow('Apply as', self.applyModeCombo)
        layout.addLayout(form)

        # 操作ガイド
        self.hint = QLabel(
            'Press Start, then grab an old node and release at the new '
            'position to add control points.')
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet('color: #555;')
        layout.addWidget(self.hint)

        # CRS 不一致の常時警告（赤文字・クリックで詳細）。Start ボタンの直上に置く
        self.crsWarn = QLabel()
        self.crsWarn.setWordWrap(True)
        self.crsWarn.setOpenExternalLinks(False)
        self.crsWarn.linkActivated.connect(self._show_crs_detail)
        self.crsWarn.hide()
        layout.addWidget(self.crsWarn)

        # 主ボタン: 待機時は Start、セッション中は Apply（同一ボタン・大きめで目立たせる）
        self.primaryBtn = QPushButton('Start')
        self.primaryBtn.setMinimumHeight(34)
        pf = self.primaryBtn.font()
        pf.setBold(True)
        self.primaryBtn.setFont(pf)
        layout.addWidget(self.primaryBtn)

        # 副ボタン: 直前の点を取り消し / セッションをリセット（中断・GCPは残る）
        self.undoBtn = QPushButton('Undo point')
        self.resetBtn = QPushButton('Reset')
        rowSec = QHBoxLayout()
        rowSec.addWidget(self.undoBtn)
        rowSec.addWidget(self.resetBtn)
        layout.addLayout(rowSec)

        # GCP ファイル入出力
        self.saveBtn = QPushButton('Save GCPs')
        self.loadBtn = QPushButton('Load GCPs')
        rowIO = QHBoxLayout()
        rowIO.addWidget(self.saveBtn)
        rowIO.addWidget(self.loadBtn)
        layout.addLayout(rowIO)

        # プレビュー品質（Save/Load の直下）
        formQuality = QFormLayout()
        self.qualityCombo = QComboBox()
        self.qualityCombo.addItems(list(QUALITY.keys()))
        self.qualityCombo.setCurrentText(DEFAULT_QUALITY)
        formQuality.addRow('Preview quality', self.qualityCombo)
        layout.addLayout(formQuality)

        # 誤差サマリ
        self.statLabel = QLabel('RMS: -   Std dev: -   Scale: -')
        layout.addWidget(self.statLabel)

        # GCP テーブル（最下部に配置し、残り高さいっぱいに伸縮させる）
        # 列: On(チェック) / #(作成順) / X誤差 / Y誤差 / 残差
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ['On', '#', 'X error', 'Y error', 'Residual'])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        for c in (2, 3, 4):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(22)
        # 手書き編集は不可、列ヘッダクリックで値に基づきソート
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        # 既定は作成順（# 列の昇順）。ヘッダクリックで他の列にも並べ替え可
        self.table.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        layout.addWidget(self.table, 1)

        self.setWidget(root)

        # シグナル接続
        self.primaryBtn.clicked.connect(self._on_primary)
        self.undoBtn.clicked.connect(self._on_undo)
        self.resetBtn.clicked.connect(self._on_reset)
        self.saveBtn.clicked.connect(self._on_save)
        self.loadBtn.clicked.connect(self._on_load)
        self.transformCombo.currentIndexChanged.connect(self._on_transform_changed)
        self.qualityCombo.currentTextChanged.connect(self._on_quality_changed)
        self.table.itemChanged.connect(self._on_item_changed)
        self.layerCombo.layerChanged.connect(self._on_layer_changed)
        self.scopeCombo.currentIndexChanged.connect(self._update_idle_primary)
        QgsProject.instance().crsChanged.connect(self._update_crs_warning)

        self._update_enabled(False)
        self._on_layer_changed()

    # ------------------------------------------------------------------
    # セッション制御
    # ------------------------------------------------------------------
    def _current_scope(self):
        return self.scopeCombo.currentData()

    def _current_transform(self):
        # (mode, lock_scale) を返す
        return self.transformCombo.currentData()

    def _on_primary(self):
        # 待機中は Start（セッション開始）、セッション中は Apply（適用）
        if self.session is None:
            self._start_session()
        else:
            self._on_apply()

    def _on_reset(self):
        # 現在のセッションを中断（GCP リストは残す）。Apply はしない
        self._stop_session()

    def _start_session(self):
        layer = self.layerCombo.currentLayer()
        if layer is None:
            self.iface.messageBar().pushMessage(
                'Freehand Vector Georeferencer', 'Please select a target layer.',
                level=Qgis.Warning, duration=3)
            return

        scope = self._current_scope()
        if scope == 'selected' and layer.selectedFeatureCount() == 0:
            self.iface.messageBar().pushMessage(
                'Freehand Vector Georeferencer',
                'No selected features. Select features or change the scope.',
                level=Qgis.Warning, duration=4)
            return

        mode, lock = self._current_transform()
        # 新しいセッションを始めるので、保存用に残していた直近セッションは破棄
        self._last_session = None
        self.session = GeorefSession(
            self.iface, layer, scope, mode, lock,
            self.qualityCombo.currentText(),
            on_update=self._refresh_stats)
        if self.session.feature_count() == 0:
            self.iface.messageBar().pushMessage(
                'Freehand Vector Georeferencer', 'No valid geometry in the scope.',
                level=Qgis.Warning, duration=4)
            self.session.cleanup()
            self.session = None
            return

        self.maptool = GeorefMapTool(self.iface, self.session)
        self.iface.mapCanvas().setMapTool(self.maptool)
        self.session.recompute()
        self.primaryBtn.setText('Apply')
        self._update_enabled(True)
        self._lock_setup_widgets(True)
        if self._current_scope() == 'single':
            self.hint.setText(
                'The feature of the first grabbed node becomes the target. '
                'Grab an old node and release at the new position.')
        else:
            self.hint.setText(
                '{} feature(s) / ~{} vertices. Grab an old node and release '
                'at the new position.'.format(
                    self.session.feature_count(), self.session.total_vertices()))

    def _stop_session(self):
        if self.maptool:
            self.iface.mapCanvas().unsetMapTool(self.maptool)
            self.maptool.cleanup()
            self.maptool = None
        if self.session:
            # 停止後も Save できるよう、GCP を持つセッションは参照を残す
            self._last_session = self.session if self.session.gcps else None
            self.session.cleanup()
            self.session = None
        self.primaryBtn.setText('Start')
        # 待機中の Start は開始可能なときだけ押せる
        self._update_idle_primary()
        # GCP リスト/統計は次に Start するまで残す（Apply 後も参照できるように）。
        # 次の Start 時に新セッションの recompute が空の内容で上書きする。
        self._update_enabled(False)
        # GCP が残っていれば Save だけは引き続き使える
        self.saveBtn.setEnabled(self._last_session is not None)
        self._lock_setup_widgets(False)

    def _lock_setup_widgets(self, locked):
        # セッション中はレイヤ・範囲の変更を禁止（GCP の座標基準が変わるため）
        self.layerCombo.setEnabled(not locked)
        self.scopeCombo.setEnabled(not locked)

    def _update_enabled(self, active):
        for b in (self.undoBtn, self.resetBtn, self.saveBtn, self.loadBtn):
            b.setEnabled(active)

    # ------------------------------------------------------------------
    # 操作ハンドラ
    # ------------------------------------------------------------------
    def _on_undo(self):
        if self.session:
            self.session.remove_last()

    def _on_transform_changed(self):
        if self.session:
            mode, lock = self._current_transform()
            self.session.set_transform(mode, lock)

    def _on_quality_changed(self, text):
        if self.session:
            self.session.set_quality(text)

    def _on_apply(self):
        if not self.session:
            return
        mode = self.applyModeCombo.currentData()
        name = self.session.layer.name()
        if mode == 'new':
            out = self.session.apply()
            if out is None:
                self._warn_no_gcp()
                return
            text = 'Created layer "{}".'.format(out.name())
        elif mode == 'add':
            n = self.session.apply_add()
            if n is None:
                self._warn_apply_failed()
                return
            text = ('Added {} feature(s) to "{}". Layer is in edit mode — '
                    'undo with Ctrl+Z, then save to keep.'.format(n, name))
        elif mode == 'edit':
            n = self.session.apply_edit()
            if n is None:
                self._warn_apply_failed()
                return
            text = ('Edited {} feature(s) in "{}". Layer is in edit mode — '
                    'undo with Ctrl+Z, then save to keep.'.format(n, name))
        else:
            return
        # 適用のたびに GCP を一時 CSV へ自動保存し、開くボタン付きで通知する
        path = self._autosave_gcps()
        self._notify_apply(text, path)
        # 適用後はセッションを終了し、別レイヤ/範囲を選べるようにする
        self._stop_session()

    def _autosave_gcps(self):
        '''適用時の GCP を一時ファイル(CSV)へ保存し、パスを返す。失敗時 None。'''
        sess = self.session
        if not sess or not sess.gcps:
            return None
        safe = ''.join(c if c.isalnum() else '_' for c in sess.layer.name())
        fname = 'fvg_gcp_{}_{}.csv'.format(
            safe, time.strftime('%Y%m%d_%H%M%S'))
        path = os.path.join(tempfile.gettempdir(), fname)
        try:
            sess.save_gcps(path)
        except OSError:
            return None
        return path

    def _notify_apply(self, text, csv_path):
        '''結果をメッセージバーに表示。CSV があればパスをログにも出し、
        保存先フォルダを開くボタンを付ける。'''
        mb = self.iface.messageBar()
        if csv_path:
            text = text + '  (GCP CSV: {})'.format(csv_path)
            # ログメッセージパネルにもパスを記録する
            QgsMessageLog.logMessage(
                'GCP CSV saved: {}'.format(csv_path),
                'Freehand Vector Georeferencer', Qgis.Info)
        item = mb.createMessage('Freehand Vector Georeferencer', text)
        if csv_path:
            folder = os.path.dirname(csv_path)
            btn = QPushButton('Open folder')
            btn.clicked.connect(
                lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(folder)))
            item.layout().addWidget(btn)
        mb.pushWidget(item, Qgis.Success, 8)

    def _warn_no_gcp(self):
        self.iface.messageBar().pushMessage(
            'Freehand Vector Georeferencer',
            'No active control points.', level=Qgis.Warning, duration=3)

    def _warn_apply_failed(self):
        self.iface.messageBar().pushMessage(
            'Freehand Vector Georeferencer',
            'Cannot apply: no active control points, or the layer is not '
            'editable.', level=Qgis.Warning, duration=4)

    def _on_save(self):
        # 実行中セッション、なければ停止済みの直近セッションを使う
        sess = self.session or self._last_session
        if not sess or not sess.gcps:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, 'Save GCPs', '', 'CSV (*.csv)')
        if not path:
            return
        if not path.lower().endswith('.csv'):
            path += '.csv'
        sess.save_gcps(path)
        self.iface.messageBar().pushMessage(
            'Freehand Vector Georeferencer', 'GCPs saved.',
            level=Qgis.Success, duration=3)

    def _on_load(self):
        if not self.session:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, 'Load GCPs', '', 'CSV (*.csv)')
        if not path:
            return
        n = self.session.load_gcps(path)
        if n == 0:
            self.iface.messageBar().pushMessage(
                'Freehand Vector Georeferencer',
                'No GCPs found in the file.', level=Qgis.Warning, duration=3)
        else:
            self.iface.messageBar().pushMessage(
                'Freehand Vector Georeferencer',
                'Loaded {} GCPs.'.format(n), level=Qgis.Success, duration=3)

    def _on_item_changed(self, item):
        if self._syncing or not self.session or item.column() != 0:
            return
        idx = item.data(Qt.ItemDataRole.UserRole)
        if idx is None:
            return
        active = item.checkState() == Qt.CheckState.Checked
        self.session.set_active(idx, active)

    # ------------------------------------------------------------------
    # 統計・テーブル更新（セッションからのコールバック）
    # ------------------------------------------------------------------
    def _refresh_stats(self, data):
        self._syncing = True
        # 再描画中はソートを止め（行挿入の度の並べ替えを防ぐ）、最後に戻す
        self.table.setSortingEnabled(False)
        rows = data['rows']
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            # 0列: ON/OFF チェック（テキストなし）。UserRole に GCP の元 index
            chk = _CheckItem('')
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable |
                         Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(
                Qt.CheckState.Checked if row['active'] else Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, row['index'])
            self.table.setItem(r, 0, chk)

            # 1列: 作成順（#番号）。数値順ソート用の値も持たせる
            num = _NumItem('#{}'.format(row['index'] + 1), row['index'] + 1)
            self.table.setItem(r, 1, num)

            ex = '' if row['ex'] is None else '{:.3f}'.format(row['ex'])
            ey = '' if row['ey'] is None else '{:.3f}'.format(row['ey'])
            mag = '' if row['mag'] is None else '{:.3f}'.format(row['mag'])
            mag_item = _NumItem(mag, row['mag'])
            if row['mag'] is not None:
                mag_item.setForeground(
                    residual_color(row['mag'], data.get('max_mag', 0.0)))
            self.table.setItem(r, 2, _NumItem(ex, row['ex']))
            self.table.setItem(r, 3, _NumItem(ey, row['ey']))
            self.table.setItem(r, 4, mag_item)

        self.table.setSortingEnabled(True)
        self.statLabel.setText(
            'RMS: {:.3f}   Std dev: {:.3f}   Scale: ×{:.4f}'.format(
                data['rms'], data['std'], data.get('scale', 1.0)))
        # セッション中の Apply は有効 GCP が 1 つ以上あるときだけ押せる
        if self.session is not None:
            active = sum(1 for r in rows if r['active'])
            self.primaryBtn.setEnabled(active > 0)
        self._syncing = False

    def closeEvent(self, event):
        self._stop_session()
        super().closeEvent(event)
