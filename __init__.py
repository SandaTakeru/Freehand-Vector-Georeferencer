# -*- coding: utf-8 -*-

# noinspection PyPep8Naming
def classFactory(iface):  # pylint: disable=invalid-name
    from .freehand_vector_georeferencer import FreehandVectorGeoreferencer
    return FreehandVectorGeoreferencer(iface)
