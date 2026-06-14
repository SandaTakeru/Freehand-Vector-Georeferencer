# -*- coding: utf-8 -*-
'''Freehand Vector Georeferencer プラグイン本体。

ベクタレイヤを直感的に既知点へ合わせ込むジオリファレンスツール。
ツールバーのアイコンからドックパネルを開いて操作する。

謝辞 / Acknowledgment:
  本プラグインのインタラクティブUI（マップツール、ラバーバンドによる
  プレビュー、ノードを掴んで動かす操作感）は、Guilhem Vellut 氏による
  "Freehand Raster Georeferencer" プラグイン
  (https://github.com/gvellut/FreehandRasterGeoreferencer) に着想を得ています。
  素晴らしい先行プラグインに深く感謝します。
'''

import os.path

from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction


class FreehandVectorGeoreferencer(object):
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.menu = '&Freehand Vector Georeferencer'
        self.action = None
        self.toolbar = None
        self.dock = None

    def initGui(self):
        # 先にアクションとツールバーアイコンを用意する（ドック生成は遅延し、
        # 万一の失敗でもツールバーからアイコンが消えないようにする）
        icon = QIcon(os.path.join(self.plugin_dir, 'icon.png'))
        self.action = QAction(icon, 'Freehand Vector Georeferencer',
                              self.iface.mainWindow())
        self.action.setObjectName('FreehandVectorGeoreferencer_Action')
        self.action.setCheckable(True)
        self.action.triggered.connect(self.toggle_dock)

        # 専用ツールバーにアイコンを表示
        self.toolbar = self.iface.addToolBar('Freehand Vector Georeferencer')
        self.toolbar.setObjectName('FreehandVectorGeoreferencerToolbar')
        self.toolbar.addAction(self.action)
        self.iface.addPluginToVectorMenu(self.menu, self.action)

    def unload(self):
        if self.dock:
            self.dock._stop_session()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removePluginVectorMenu(self.menu, self.action)
        # ツールバー/アクションは即時破棄する（再読込時に objectName が重複し
        # 「duplicated widget not cleaned up」警告が出るのを防ぐ）
        if self.toolbar is not None:
            self.iface.mainWindow().removeToolBar(self.toolbar)
            sip.delete(self.toolbar)
            self.toolbar = None
        if self.action is not None:
            sip.delete(self.action)
            self.action = None

    def _ensure_dock(self):
        # ドックは初回使用時に生成する
        if self.dock is None:
            from .georef_dockwidget import GeorefDockWidget
            self.dock = GeorefDockWidget(self.iface, self.iface.mainWindow())
            self.iface.addDockWidget(
                Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
            self.dock.visibilityChanged.connect(self.action.setChecked)

    def toggle_dock(self, checked):
        if checked:
            self._ensure_dock()
            # アイコンを押して開くたびに、現在のアクティブレイヤを既定にする
            self.dock._preselect_active_layer()
            self.dock.show()
            self.dock.raise_()
        elif self.dock:
            self.dock.hide()
