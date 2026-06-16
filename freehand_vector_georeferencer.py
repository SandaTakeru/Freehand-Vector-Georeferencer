# -*- coding: utf-8 -*-
'''Freehand Vector Georeferencer main plugin.

An intuitive georeferencing tool that aligns vector layers to known points.
Open the dock panel from the toolbar icon to operate it.

Acknowledgment:
  The interactive UI of this plugin (the map tool, the rubber-band preview and
  the "grab a node and move it" feel) is inspired by the
  "Freehand Raster Georeferencer" plugin by Guilhem Vellut
  (https://github.com/gvellut/FreehandRasterGeoreferencer).
  Many thanks for that great earlier work.
'''

import os.path

from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QIcon, QDesktopServices
from qgis.PyQt.QtWidgets import QAction, QMenu, QToolButton


class FreehandVectorGeoreferencer(object):
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.menu = '&Freehand Vector Georeferencer'
        self.action = None
        self.help_action = None
        self.tool_button = None
        self.tool_menu = None
        self.toolbar = None
        self.dock = None
        # ▼メニューから開く使い方ページ（動画はこのREADME上で再生できる）
        self.help_url = ('https://github.com/SandaTakeru/'
                         'Freehand-Vector-Georeferencer/blob/main/README.md')

    def initGui(self):
        # Create the action and toolbar icon first; the dock is created lazily
        # so the toolbar icon never disappears even if dock creation fails.
        icon = QIcon(os.path.join(self.plugin_dir, 'icon.png'))
        self.action = QAction(icon, 'Freehand Vector Georeferencer',
                              self.iface.mainWindow())
        self.action.setObjectName('FreehandVectorGeoreferencer_Action')
        self.action.setCheckable(True)
        self.action.triggered.connect(self.toggle_dock)

        # ▼メニュー：使い方ページ（動画）をブラウザで開く
        self.help_action = QAction('How to use (watch video)',
                                   self.iface.mainWindow())
        self.help_action.triggered.connect(self.open_help)

        self.tool_menu = QMenu(self.iface.mainWindow())
        self.tool_menu.addAction(self.help_action)

        # アイコン本体クリックでドック開閉、右の▼でメニュー（地物選択と同じ作り）
        self.tool_button = QToolButton()
        self.tool_button.setDefaultAction(self.action)
        self.tool_button.setMenu(self.tool_menu)
        self.tool_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.MenuButtonPopup)

        # Show the icon on a dedicated toolbar.
        self.toolbar = self.iface.addToolBar('Freehand Vector Georeferencer')
        self.toolbar.setObjectName('FreehandVectorGeoreferencerToolbar')
        self.toolbar.addWidget(self.tool_button)
        self.iface.addPluginToVectorMenu(self.menu, self.action)
        self.iface.addPluginToVectorMenu(self.menu, self.help_action)

    def open_help(self):
        QDesktopServices.openUrl(QUrl(self.help_url))

    def unload(self):
        if self.dock:
            self.dock._stop_session()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removePluginVectorMenu(self.menu, self.action)
        if self.help_action is not None:
            self.iface.removePluginVectorMenu(self.menu, self.help_action)
        # Destroy the toolbar/action immediately so that on reload the
        # objectName is not duplicated (avoids the "duplicated widget not
        # cleaned up" warning).
        if self.toolbar is not None:
            self.iface.mainWindow().removeToolBar(self.toolbar)
            sip.delete(self.toolbar)
            self.toolbar = None
        # tool_button/tool_menu はツールバー破棄で一緒に消えるが、参照を切る
        self.tool_button = None
        self.tool_menu = None
        if self.help_action is not None:
            sip.delete(self.help_action)
            self.help_action = None
        if self.action is not None:
            sip.delete(self.action)
            self.action = None

    def _ensure_dock(self):
        # Create the dock on first use.
        if self.dock is None:
            from .georef_dockwidget import GeorefDockWidget
            self.dock = GeorefDockWidget(self.iface, self.iface.mainWindow())
            self.iface.addDockWidget(
                Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
            self.dock.visibilityChanged.connect(self.action.setChecked)

    def toggle_dock(self, checked):
        if checked:
            self._ensure_dock()
            # Each time the icon opens the dock, default to the active layer.
            self.dock._preselect_active_layer()
            self.dock.show()
            self.dock.raise_()
        elif self.dock:
            self.dock.hide()
