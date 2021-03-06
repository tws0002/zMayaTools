# This implements the keying UI for zMouthController nodes.

import glob, os, sys, time, traceback, threading
from pprint import pprint, pformat
import pymel.core as pm
import maya
from maya import OpenMaya as om
from maya.app.general import mayaMixin
from maya.app.general.mayaMixin import MayaQWidgetDockableMixin
from zMayaTools import maya_helpers, maya_logging, Qt, qt_helpers
reload(qt_helpers)

import maya.OpenMayaUI as omui
from maya.OpenMaya import MGlobal

log = maya_logging.get_log()

# XXX: dragging from the outliner into the tree doesn't allow dropping into a position

def get_controller_for_object(node):
    connections = node.message.listConnections(s=False, d=True, type='controller')
    if connections:
        return connections[0]
    return None

def get_controller_object(controller):
    """
    Get the object associated with a controller, or None if there's no object,
    which is usually a group.
    """
    controller_objects = controller.controllerObject.listConnections(s=True, d=False)
    return controller_objects[0] if controller_objects else None

def get_controller_parent(controller, get_plug=False):
    connections = controller.parent.listConnections(s=False, d=True, t='controller', p=get_plug)
    if connections:
        return connections[0]
    else:
        return None
    
def get_controller_children(parent, disconnect_from_parent=False):
    """
    Return a list of all children of the given controller.

    If disconnect_from_parent is true, disconnect the children list so edits can be
    made.  Reassign the resulting list using assign_controller_children.
    """
    all_children = []
    for children_plug in parent.children:
        children = children_plug.listConnections(s=True, d=False, t='controller', p=True)
        if not children:
            continue
        child = children[0]
        all_children.append(child.node())
        if disconnect_from_parent:
            child.disconnect(children_plug)

    return all_children

def assign_controller_children(parent, children):
    """
    Assign a controller's child list.

    This is always called after disconnecting the list using
    get_controller_children(disconnect_from_parent=True).  This doesn't connect
    the prepopulate attribute.
    """
    for idx, child in enumerate(children):
        child.parent.connect(parent.children[idx])

def _remove_gaps_in_child_list(controller):
    """
    If there are gaps in the children array of a controller, pick walking doesn't
    work.  Remove any gaps in the given controller's child list, preserving the
    order of connections.
    """
    connections = []
    any_gaps_seen = False
    for entry in controller.children:
        if not entry.isConnected():
            any_gaps_seen = True
            continue
        connection = pm.listConnections(entry, s=True, d=False, p=True)[0]
        connections.append(connection)

    # If there weren't any gaps, we don't need to make any changes.
    if not any_gaps_seen:
        return

    for entry in controller.children:
        for connection in pm.listConnections(entry, s=True, d=False, p=True):
            connection.disconnect(entry)

    for idx, connection in enumerate(connections):
        connection.connect(controller.children[idx])

def remove_gaps_in_all_controllers():
    """
    Call remove_gaps_in_child_list in all non-referenced controllers to correct
    any existing gaps.

    This is only needed to correct any bad controller connections created by earlier
    versions.
    """
    for controller in pm.ls(type='controller'):
        if pm.referenceQuery(controller, isNodeReferenced=True):
            continue

        _remove_gaps_in_child_list(controller)

def set_controller_parent(controller, parent, after=None):
    """
    Set controller's parent to parent.

    controller -p should do this, but the docs don't really describe how it works and
    there doesn't seem to be any way to change the parent of a controller to be a root.
    """
    with maya_helpers.undo():
        old_parent_plug = get_controller_parent(controller, get_plug=True)
        if old_parent_plug is None and parent is None:
            return

        if old_parent_plug is not None:
            # Unparent the controller from its old parent.
            old_parent_plug.node().prepopulate.disconnect(controller.prepopulate)

            # We need to reconnect all children and not just disconnect the child, since the
            # child list needs to remain contiguous.
            all_children = get_controller_children(old_parent_plug.node(), disconnect_from_parent=True)
            all_children.remove(controller.node())
            assign_controller_children(old_parent_plug.node(), all_children)

        # If we're unparenting, we're done.
        if parent is None:
            return

        # Connect prepopulate.
        parent.prepopulate.connect(controller.prepopulate)

        # Get a list of all children of the parent, and disconnect them.
        all_children = get_controller_children(parent, disconnect_from_parent=True)

        # Insert our new child.  If we've been given a position to add it, use it.  Otherwise,
        # add to the beginning.
        if after is None or after not in all_children:
            all_children = [controller] + all_children
        else:
            idx = all_children.index(after) + 1
            all_children[idx:idx] = [controller]

        # Reconnect the children.
        assign_controller_children(parent, all_children)

class ControllerListener(object):
    """
    Listen for changes to controllers, so we can refresh the UI.
    """
    def __init__(self):
        self.callback_ids = om.MCallbackIdArray()
        self.registered = False
        self.change_callback = None
        self.selection_changed_callback = None

    def set_changed_callback(self, callback):
        """
        Set the callable to run when controllers change.
        """
        self.change_callback = callback

    def set_selection_changed_callback(self, callback):
        """
        Set the callable to run when the selection changes.
        """
        self.selection_changed_callback = callback

    def __del__(self):
        self.unregister()

    def register(self):
        if self.registered:
            return
        self.registered = True
        
        self.callback_ids.append(om.MEventMessage.addEventCallback('SelectionChanged', self._selection_changed))

        msg = om.MDGMessage()
        self.callback_ids.append(msg.addNodeAddedCallback(self._node_added, 'controller', None))
        self.callback_ids.append(msg.addNodeRemovedCallback(self._node_removed, 'controller', None))
        for node in pm.ls(type='controller'):
            self.callback_ids.append(om.MNodeMessage.addAttributeChangedCallback(node.__apimobject__(), self._attribute_changed, None))

            # Listen for controller objects being renamed, so we update node names.
            controller_object = get_controller_object(node)
            if controller_object is not None:
                self.callback_ids.append(om.MNodeMessage.addNameChangedCallback(controller_object.__apimobject__(), self._node_renamed))

    def unregister(self):
        if not self.registered:
            return
        self.registered = False

        msg = om.MMessage()
        msg.removeCallbacks(self.callback_ids)
        self.callback_ids.clear()

    def _reregister(self):
        if not self.registered:
            return

        self.unregister()
        self.register()

    def _attribute_changed(self, msg, plug, otherPlug, data):
        if msg & (om.MNodeMessage.kConnectionMade | om.MNodeMessage.kConnectionBroken):
            qt_helpers.run_async_once(self._run_change_callback)

    def _node_added(self, node, data):
        qt_helpers.run_async_once(self._reregister)
        qt_helpers.run_async_once(self._run_change_callback)

    def _node_removed(self, node, data):
        qt_helpers.run_async_once(self._reregister)
        qt_helpers.run_async_once(self._run_change_callback)

    def _node_renamed(self, node, old_name, unused):
        qt_helpers.run_async_once(self._run_change_callback)

    def _run_change_callback(self):
        if self.change_callback:
            self.change_callback()

    def _selection_changed(self, unused):
        qt_helpers.run_async_once(self._run_selection_changed_callback)

    def _run_selection_changed_callback(self):
        if self.selection_changed_callback:
            self.selection_changed_callback()

class ControllerEditor(MayaQWidgetDockableMixin, Qt.QDialog):
    def done(self, result):
        self.close()
        super(MayaQWidgetDockableMixin, self).done(result)

    def _check_listeners(self):
        if not self.shown:
            # If we're listening, stop.
            self.listener.unregister()
            return

        self.listener.register()

    def __init__(self):
        super(ControllerEditor, self).__init__()

        self.controllers_to_items = {}
        self.currently_refreshing = False

        self.listener = ControllerListener()
        self.listener.set_changed_callback(self.populate_tree)
        self.listener.set_selection_changed_callback(self.select_selected_controller)

        # How do we make our window handle global hotkeys?
        undo = Qt.QAction('Undo', self)
        undo.setShortcut(Qt.Qt.CTRL + Qt.Qt.Key_Z)
        undo.triggered.connect(lambda: pm.undo())
        self.addAction(undo)

        redo = Qt.QAction('Redo', self)
        redo.setShortcut(Qt.Qt.CTRL + Qt.Qt.Key_Y)
        redo.triggered.connect(lambda: pm.redo(redo=True))
        self.addAction(redo)

        self.shown = False
        self.callback_ids = om.MCallbackIdArray()

        style = r'''
        /* Maya's checkbox style makes the checkbox invisible when it's deselected,
         * which makes it impossible to tell that there's even a checkbox there to
         * click.  Adjust the background color to fix this. */
        QTreeView::indicator:unchecked {
            background-color: #000;
        }
        '''
        self.setStyleSheet(style)

        qt_helpers.compile_all_layouts()

        from zMayaTools.qt_widgets import controller_tree_widget
        reload(controller_tree_widget)

        from zMayaTools.qt_generated import zControllerEditor
        reload(zControllerEditor)

        self.ui = zControllerEditor.Ui_zControllerEditor()
        self.ui.setupUi(self)

        self.ui.controllerTree.setSelectionMode(Qt.QAbstractItemView.SingleSelection)
        self.ui.controllerTree.setDragEnabled(True)
        self.ui.controllerTree.viewport().setAcceptDrops(True)
        self.ui.controllerTree.setDropIndicatorShown(True)
        self.ui.controllerTree.setDragDropMode(Qt.QAbstractItemView.InternalMove)
        self.ui.controllerTree.setColumnCount(4)
        self.ui.controllerTree.header().setSectionResizeMode(Qt.QHeaderView.ResizeToContents)

        self.ui.controllerTree.dragged_internally.connect(self.dragged_internally)
        self.ui.controllerTree.dragged_from_maya.connect(self.dragged_from_maya)

        self.ui.controllerTree.itemSelectionChanged.connect(self.selection_changed)
        self.ui.controllerTree.itemClicked.connect(self.selection_changed)

        self.ui.createControllerButton.clicked.connect(self.create_controller_for_selected_object)
        self.ui.createControllerGroupButton.clicked.connect(self.create_controller_group)
        self.ui.deleteButton.clicked.connect(self.delete_controller)

        self.populate_tree()

    def selection_changed(self):
        # Ignore selection changes while populate_tree is refreshing the tree.
        if self.currently_refreshing:
            return

        # As the tree view selection changes, select the associated object.
        selected_controllers = [item.controller_node for item in self.ui.controllerTree.selectedItems()]
        if not selected_controllers:
            return

        node = get_controller_object(selected_controllers[0])        
        if node is None:
            return

        # Don't call pm.select if the selection isn't changing.  We get multiple callbacks
        # for each click, so this reduces undo cruft.
        if pm.ls(sl=True) == [node]:
            return
        pm.select(node, ne=True)

    def create_controller_for_selected_object(self):
        nodes = pm.ls(sl=True, type='transform')
        if not nodes:
            pm.info('Select one or more object to create a controller for')
            return

        with maya_helpers.undo():
            self.create_controller_for_objects(nodes)

    def create_controller_group(self):
        with maya_helpers.undo():
            self.create_controller_for_objects([None])

    def dragged_from_maya(self, nodes, target, indicator_position):
        with maya_helpers.undo():
            # Create controllers for the nodes if they don't exist.
            self.create_controller_for_objects(nodes)

            for node in nodes:
                # See if there's a controller for this node.
                controller = get_controller_for_object(node)
                if not controller:
                    continue

                self.dragged_controller(controller, target.controller_node if target is not None else None, indicator_position)

    def create_controller_for_objects(self, nodes):
        """
        Create controllers.  nodes is a list of controller objects, which must be transforms.

        If an entry in nodes is None, create a controller without any object, which can be
        used as a group.  This is what you get from pm.controller(g=True).

        Note that we don't use pm.controller, since it doesn't return the new node like most
        object creation functions do.
        """
        # Remember the selection.  We don't want to leave the new controller node selected
        # when we're done.
        old_selection = pm.ls(sl=True)


        first_node = None
        for node in nodes:
            # If we have a node and it already has a controller, don't create another one.
            if node is not None:
                existing_controller = get_controller_for_object(node)
                if existing_controller:
                    first_node = existing_controller
                    continue

            name = 'controller#'
            if node is not None:
                name = '%s_Tag' % node.nodeName()
            controller = pm.createNode('controller', n=name)

            if node is not None:
                node.message.connect(controller.controllerObject)

            if first_node is None:
                first_node = controller

        # Refresh the list now, so we can select the new controller.
        self.populate_tree()

        # We don't allow multiple selection in the tree view, so just select one of the
        # controllers we created.
        item = self.controllers_to_items.get(first_node)
        self.ui.controllerTree.setCurrentItem(item)

        pm.select(old_selection, ne=True)

    def delete_controller(self):
        # Delete the selection.  This will automatically delete any controllers underneath
        # it.
        selected_controllers = [item.controller_node for item in self.ui.controllerTree.selectedItems()]
        parents = []
        for controller in selected_controllers:
            connections = controller.parent.listConnections(s=False, d=True)
            if connections:
                parents.append(connections[0])
        pm.delete(selected_controllers)

        # When controllers are deleted, they leave behind gaps in the controllers they were connected
        # to.  This breaks pick walking.  Work around this by defragmenting any controller that used
        # to be a parent of a deleted controller.
        for controller in parents:
            # Skip this controller if it doesn't exist, since it might have been deleted due to
            # us deleting its only connection.
            if not controller.exists():
                continue
            _remove_gaps_in_child_list(controller)

    def dragged_internally(self, source, target, indicator_position):
        self.dragged_controller(source.controller_node, target.controller_node if target is not None else None, indicator_position)

    def dragged_controller(self, source, target, indicator_position):
        if indicator_position == Qt.QAbstractItemView.DropIndicatorPosition.OnViewport:
            # Dragging an object onto nothing makes it a root.
            set_controller_parent(source, None)
            return

        if indicator_position == Qt.QAbstractItemView.DropIndicatorPosition.OnItem:
            set_controller_parent(source, target)
            return

        if indicator_position == Qt.QAbstractItemView.DropIndicatorPosition.BelowItem:
            # Dragging below an item can happen whether there are children or not.  If there
            # are children, then parent the source into the target, making it the first child.
            # If there aren't, make it the next sibling of the target.
            target_has_children = len(get_controller_children(target)) > 0
            if target_has_children:
                set_controller_parent(source, target)
            else:
                target_parent = get_controller_parent(target)
                if target_parent is None:
                    return
                    
                set_controller_parent(source, target_parent, after=target)

        if indicator_position == Qt.QAbstractItemView.DropIndicatorPosition.AboveItem:
            target_parent = get_controller_parent(target)
            if target_parent is None:
                return
                
            # Move the source to the same parent as the target, placing it before the target.
            children = get_controller_children(target_parent)
            target_position = children.index(target)
            if target_position == 0:
                # Place it at the beginning.
                after_node = None
            else:
                after_node = children[target_position-1]
            set_controller_parent(source, target_parent, after=after_node)

    def populate_tree(self):
        # Don't update the scene selection to follow the tree selection as we clear and recreate
        # the tree, or it'll create a bunch of undo entries.
        self.currently_refreshing = True

        # Remember the selection and expanded nodes, so we can restore it later if the selected node still exists.
        selected_controllers = [item.controller_node for item in self.ui.controllerTree.selectedItems()]
        old_nodes = {node: item.isExpanded() for node, item in self.controllers_to_items.iteritems()}
        
        self.ui.controllerTree.clear()
        self.controllers_to_items = {}

        # Find all root controllers.
        roots = [controller for controller in pm.ls(type='controller') if not controller.parent.isConnected()]

        seen_nodes = set()
        def add_controllers_recursively(node, parent, level=0):
            # Controllers don't actually support cycles (they seem to, but crash when you
            # try to use them), but the DG connections can still have cycles.  Check for
            # this to avoid infinite recursion.
            if node in seen_nodes:
                log.warning('Controller cycle: %s', node)
                return
            seen_nodes.add(node)

            item = Qt.QTreeWidgetItem(parent)

            item.controller_node = node
            self.controllers_to_items[node] = item

            item.controller_object = get_controller_object(node)
            name = item.controller_object.nodeName() if item.controller_object else item.controller_node.nodeName()

            item.setText(0, name)
            item.setFlags(Qt.Qt.ItemIsEnabled | Qt.Qt.ItemIsSelectable | Qt.Qt.ItemIsDragEnabled | Qt.Qt.ItemIsDropEnabled)

            if parent is self.ui.controllerTree:
                self.ui.controllerTree.addTopLevelItem(item)
            else:
                parent.addChild(item)

            # Expand the node if it's new or if it was expanded previously.
            if old_nodes.get(node, True):
                self.ui.controllerTree.expandItem(item)
 
             # If this controller was selected, reselect it.
            if node in selected_controllers:
                self.ui.controllerTree.setCurrentItem(item)

            # Add the controller's children.
            children = node.children.listConnections(s=True, d=False)
            for child in children:
                add_controllers_recursively(child, item, level+1)

        for root in roots:
            add_controllers_recursively(root, self.ui.controllerTree)

        self.currently_refreshing = False

    def _async_refresh(self):
        """
        Queue a refresh.  If this is called multiple times before we do the refresh, we'll only
        refresh once.
        """
        qt_helpers.run_async_once(self.refresh)

    def __del__(self):
        self.cleanup()

    def cleanup(self):
        self.listener.unregister()

    def showEvent(self, event):
        # Why is there no isShown()?
        if self.shown:
            return
        self.shown = True

        # Refresh when we're displayed.
        self._check_listeners()

        super(ControllerEditor, self).showEvent(event)

    def hideEvent(self, event):
        if not self.shown:
            return
        self.shown = False

        self._check_listeners()

        super(ControllerEditor, self).hideEvent(event)

    def dockCloseEventTriggered(self):
        # Bug workaround: closing the dialog by clicking X doesn't call closeEvent.
        self.cleanup()
    
    def close(self):
        self.cleanup()
        super(ControllerEditor, self).close()

    def select_selected_controller(self):
        # If a transforom is selected that has a controller, select the controller
        # in the tree.
        nodes = pm.ls(sl=True, type='transform')
        if not nodes:
            return

        for node in nodes:
            controllers = node.message.listConnections(s=False, d=True, type='controller')
            if not controllers:
                continue

            item = self.controllers_to_items.get(controllers[0])
            if item is None:
                continue

            # Set currently_refreshing while we set the selection, so selection_changed doesn't try
            # to sync Maya's selection list to the UI.  We're syncing in the other direction, and
            # that causes multiple Maya selections to be reduced to one selection (since our tree
            # view has multiple selection disabled), causing problems with pick walking.
            self.currently_refreshing = True
            try:
                self.ui.controllerTree.setCurrentItem(item)
            finally:
                self.currently_refreshing = False
            break
        
