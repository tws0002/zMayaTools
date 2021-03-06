import contextlib, functools, logging, time
from collections import namedtuple
from pymel import core as pm
from maya import OpenMaya as om
from zMayaTools import util
import maya.OpenMayaUI as omui

from zMayaTools import maya_logging
log = maya_logging.get_log()

var_type = namedtuple('var_type', ('expected_type','maya_type'))
class OptionVar(object):
    """
    A helper for accessing a single optionvar.

    test = maya_helpers.OptionVar('test', 'bool', True)    
    print test.value
    test.value = False
    print test.value
    """

    _types = {
        'int': var_type(expected_type=int, maya_type='intValue'),
        'float': var_type(expected_type=(float, int), maya_type='floatValue'),
        'string': var_type(expected_type=basestring, maya_type='stringValue'),
        'bool': var_type(expected_type=(bool, int), maya_type='intValue'),
    }
    
    def __init__(self, name, var_type, default):
        """
        Note that on_change is only called if the optvar is changed via this object,
        since Maya has no way to register a callback when an optvar changes.
        """
        assert var_type in self._types
        self.name = name
        self.var_type = var_type
        self.default = default
        self.on_change_callbacks = []

    def __str__(self):
        return 'OptionVar(%s=%s)' % (self.name, self.value)

    def reset(self):
        """
        Reset the optvar to its default value.

        This just removes the var, so value will return the default.
        """
        pm.optionVar(remove=self.name)
        self._call_on_change()

    def _call_on_change(self):
        for callback in list(self.on_change_callbacks):
            try:
                callback(self)
            except Exception as e:
                # The callback may have nothing to do with the code changing the value, so
                # don't propagate the exception upwards.  Just log it.
                log.exception('on_change raised exception')

    def add_on_change_listener(self, on_change):
        self.on_change_callbacks.append(on_change)

    def remove_on_change_listener(self, on_change):
        if on_change in self.on_change_callbacks:
            self.on_change_callbacks.remove(on_change)

    @property
    def value(self):
        if not pm.optionVar(exists=self.name):
            return self.default

        value = pm.optionVar(q=self.name)

        # Make sure the value is of the type we expect.  If it's not, return the default instead.
        item_type = self._types[self.var_type]
        expected_class = item_type.expected_type
        if not isinstance(value, expected_class):
            return self.default

        # For bool, cast to bool.
        if self.var_type == 'bool':
            value = bool(value)

        return value

    @value.setter
    def value(self, value):
        old_value = self.value

        item_type = self._types[self.var_type]
        expected_class = item_type.expected_type
        assert isinstance(value, expected_class), 'Option %s has type %s and can\'t be set to "%s"' % (self.name, self.var_type, value)

        kwargs = {}
        arg = item_type.maya_type
        kwargs[arg] = (self.name, value)
        pm.optionVar(**kwargs)

        if value != old_value:
            self._call_on_change()

class OptionVars(object):
    """
    A helper to simplify accessing Maya optionvars.

    This supports ints, floats and strings (arrays aren't supported).
    """
    def __init__(self):
        self.keys = {}

    def add(self, name, *args, **kwargs):
        self.keys[name] = OptionVar(name, *args, **kwargs)

    def add_from(self, optvars):
        """
        Add all optvars from another OptionVars object.
        """
        self.keys.update(optvars.keys)

    def get(self, name):
        """
        Return the OptionVar for the given key.
        """
        return self.keys[name]

    def reset(self):
        for option in self.keys.values():
            option.reset()

    def __setitem__(self, name, value):
        option = self.keys.get(name)
        assert option is not None, 'Unknown option var name %s' % name
        option.value = value

    def __getitem__(self, name):
        option = self.keys.get(name)
        assert option is not None, 'Unknown option var name %s' % name

        return option.value

class OptionsBox(object):
    def __init__(self):
        self.optvars = OptionVars()

    # Set this to the title of the option box.
    title = '(not set)'

    def run(self):
        pm.setParent(pm.mel.eval('getOptionBox()'))
        
        pm.setUITemplate('DefaultTemplate', pushTemplate=True)

        pm.waitCursor(state=1)

        pm.tabLayout(tabsVisible=False, scrollable=True)
        
        self.options_box_setup()
        self.option_box_load()

        pm.waitCursor(state=0)
        
        pm.setUITemplate(popTemplate=True)

        # We need to set both apply and apply and close explicitly.  Maya breaks apply and close
        # if apply is set to a Python function.
        def apply(unused):
            self.option_box_save()
            self.option_box_apply()

        def apply_and_close(unused):
            self.option_box_save()
            self.option_box_apply()
            pm.mel.eval('hideOptionBox()')
            
        pm.button(pm.mel.eval('getOptionBoxApplyBtn()'), edit=True, command=apply)
        pm.button(pm.mel.eval('getOptionBoxApplyAndCloseBtn()'), edit=True, command=apply_and_close)
    
        # XXX: Is there a way for us to add a help link?
        pm.mel.eval('setOptionBoxCommandName("%s")' % self.title)
        # pm.mel.eval('setOptionBoxHelpTag "%s"' % self.title)
        pm.mel.eval('setOptionBoxTitle("%s");' % self.title)

        pm.mel.eval('showOptionBox()')

        # These need to be set directly to the menu item after showing the option box to
        # work around a Maya bug that breaks these when they're connected to Python functions.
        pm.menuItem(pm.mel.globals['gOptionBoxEditMenuSaveItem'], edit=True, command=lambda unused: self.option_box_save())
        pm.menuItem(pm.mel.globals['gOptionBoxEditMenuResetItem'], edit=True, command=lambda unused: self.option_box_reset())

    def options_box_setup(self):
        """
        Implement this to set up the options box.
        """
        raise NotImplemented

    def option_box_apply(self):
        """
        Implement this to run the action when the user clicks "Apply".
        """
        raise NotImplemented

    def option_box_save(self):
        """
        Save any persisted options to self.optvars.
        """
        raise NotImplemented

    def option_box_load(self):
        """
        Load any persisted options from self.optvars to the UI.
        """
        raise NotImplemented
        
    def option_box_reset(self):
        self.optvars.reset()
        self.option_box_load()

class ProgressWindowMaya(util.ProgressWindow):
    def __init__(self, total_progress_values, title='Progress...',
            # Show a title above the main progress bar.  (If a secondary progress bar is displayed,
            # it will always have a title.)
            with_titles=False,

            # Show a secondary progress bar, which can be updated with set_task_progress.
            with_secondary_progress=False,

            # Show a cancel button.
            with_cancel=False):
        super(ProgressWindowMaya, self).__init__()
        self.window = None
        self.last_refresh = None
        self.main_progress_value = -1
        self.with_titles = with_titles
        self.with_secondary_progress = with_secondary_progress

        self.window = pm.window(title=title, minimizeButton=False, maximizeButton=False)
        pm.columnLayout()
        
        if self.with_titles:
            pm.text('status', w=300, align='left')

        self.progressControl1 = pm.progressBar(maxValue=total_progress_values, width=300)
        self.set_total_progress_value(total_progress_values)

        if self.with_secondary_progress:
            pm.text('status2', w=300, align='left')
            self.progressControl2 = pm.progressBar(maxValue=100, width=300, pr=5)

        if with_cancel:
            pm.button(label='Cancel', command=self._cancel_clicked)

        pm.showWindow(self.window)
        pm.refresh()

        # Advance from -1 to 0.
        self.update()

    def set_total_progress_value(self, total_progress_values):
        pm.progressBar(self.progressControl1, e=True, maxValue=total_progress_values)

    def hide(self):
        super(ProgressWindowMaya, self).hide()
        
        pm.deleteUI(self.window)
        self.window = None

    def _cancel_clicked(self, unused):
        log.debug('Cancel button clicked')
        self.cancel()

    def update(self, advance_by=1, text=''):
        super(ProgressWindowMaya, self).update(advance_by, text)
        
        # Reset the sub-task refresh timer when we change the main task.
        self.last_refresh = None
        self.last_task_percent = 0
        
        if text:
            log.info(text)

        if self.window is None:
            return

        if text:
            if self.with_titles:
                pm.text('status', e=True, label=text)
            else:
                self.window = pm.window(self.window, e=True, title=text)

        pm.progressBar(self.progressControl1, edit=True, progress=self.main_progress_value)

        if self.with_secondary_progress:
            pm.text('status2', e=True, label='')
            pm.progressBar(self.progressControl2, edit=True, progress=0)

        # Hack: The window sometimes doesn't update if we don't call this twice.
        pm.refresh()
        pm.refresh()

        self.main_progress_value += advance_by

    def set_task_progress(self, label, percent=None, force=False):
        # Check for cancellation when we update progress.
        self.check_cancellation()

#        log.debug(label)

        if percent is None:
            percent = self.last_task_percent
            
        self.last_task_percent = percent

        if self.window is None:
            return

        # Only refresh if we haven't refreshed in a while.  This is slow enough that it
        # can make the import slower if we're showing fine-grained progress.
        if not force and self.last_refresh is not None and time() - self.last_refresh < 0.1:
            return

        self.last_refresh = time.time()

        pm.text('status2', e=True, label=label)
        pm.progressBar(self.progressControl2, edit=True, progress=round(percent * 100))

        pm.refresh()
        pm.refresh()

class TimeChangeListener(object):
    """
    Helper to receive a callback when the scene time changes.

    By default, callbacks are paused during playback to avoid impacting playback
    performance.  To always receive callbacks, set pause_during_playback to true.

    register() must be called to begin receiving callbacks.  unregister() must be
    called to clean up listeners before unloading code.
    """
    def __init__(self, callback, pause_during_playback=True):
        self.playing_back_job = None
        self.playback_callback_id = None
        self.callback = callback
        self._pause_during_playback = pause_during_playback

    def register(self):
        """
        Register the time changed listener if we're not currently in playback.

        We deregister this listener during playback, so we only update when scrubbing the timeline
        and don't slow down playback.
        """
        if self.playing_back_job is None:
            self.playing_back_job = pm.scriptJob(conditionTrue=('playingBack', self._playback_stopped))

        if self._enable_time_listener() and self.playback_callback_id is None:
            self._register_time_changed_listener()

    def unregister(self):
        if self.playing_back_job is not None:
            pm.scriptJob(kill=self.playing_back_job)
            self.playing_back_job = None
        
        self._unregister_time_changed_listener()

    def _time_changed(self, unused):
        # We want to unregister the time changed listener during playback, but there seems
        # to be no way to get a callback when playback starts.  scriptJob('playingBack') isn't
        # even called.
        #
        # If we unregister here, we'll register again when playback ends via scriptJob('playingBack').
        if not self._enable_time_listener():
            self._unregister_time_changed_listener()
            return

        self.callback()

    def _register_time_changed_listener(self):
        if self.playback_callback_id is not None:
            return

        self.playback_callback_id = om.MEventMessage.addEventCallback('timeChanged', self._time_changed)

    def _unregister_time_changed_listener(self):
        if self.playback_callback_id is None:
            return

        msg = om.MMessage()
        msg.removeCallback(self.playback_callback_id)
        self.playback_callback_id = None

    def _enable_time_listener(self):
        if not self._pause_during_playback:
            return True

        return not pm.play(q=True, state=True)

    def _playback_stopped(self):
        if not self._enable_time_listener():
            self._unregister_time_changed_listener()
            return
        
        # This is called when playback mode is exited (as well as when scrubbing is released, which
        # we don't care about).  Register the time changed listener, which we only use when not in
        # playback.
        self._register_time_changed_listener()

        # Make sure we refresh to show the state when playback stopped.
        self.callback()

class RestorableWindow(object):
    """
    This is a helper for creating a QT window that can be saved to the panel layout and restored when
    Maya loads.
    """
    def __init__(self, window_class, plugins=None, uiScript=None):
        self.ui = None
	self.window_class = window_class
        self.plugins = plugins
        self.uiScript = uiScript

    def _open_ui(self, restore):
        if restore:
            # We're being reopened, and a layout has already been created.
            restored_control = omui.MQtUtil.getCurrentParent()

        if self.ui is None:
            self.ui = self.window_class()
            def closed():
                self.ui = None
            self.ui.destroyed.connect(closed)

        if restore:
            # We're restoring into an existing layout.  Just add the control that was created
            # for us, and show() will be called automatically.
            ptr = omui.MQtUtil.findControl(self.ui.objectName())
            omui.MQtUtil.addWidgetToMayaLayout(long(ptr), long(restored_control))
            return

        # Disable retain, or we won't be able to create the window again after reloading the script
        # with an "Object's name 'DialogWorkspaceControl' is not unique" error.
        #
        # Watch out: this function has *args and *kwargs which should be there, which causes it to
        # silently eat unknown parameters instead of throwing an error.
        self.ui.setDockableParameters(dockable=True, retain=False,
                plugins=self.plugins, uiScript=self.uiScript )

        # If we just set plugins (which is really workspaceControl -requiredPlugin), the control
        # will be closed on launch.  We need to enable checksPlugins too to work around this.
        control_name = self.ui.objectName() + 'WorkspaceControl'
        pm.workspaceControl(control_name, e=True, checksPlugins=True)

        self.ui.show()

    def show(self):
        self._open_ui(restore=False)

    def hide(self):
        """
        Hide the UI.
        """
        if self.ui is not None:
            self.ui.hide()

    def close(self):
        if self.ui is None:
            return

        self.ui.close()
        self.ui = None

    def restore(self):
        """
        This is called by Maya via uiScript when a layout is restored.
        """
        self._open_ui(True)

@contextlib.contextmanager
def undo(name='undo_on_exception'):
    """
    Run a block of code in an undo block.
    """
    pm.undoInfo(openChunk=True, undoName=name)
    try:
        yield
    finally:
        pm.undoInfo(closeChunk=True)

@contextlib.contextmanager
def without_undo():
    """
    Run a block of code with undo disabled.

    This should only be used by operations that have no actual effect on the scene.
    It's useful if a script makes temporary changes or nodes in the scene and cleans
    them up when it's done, to avoid creating pointless undo chunks.
    """
    pm.undoInfo(stateWithoutFlush=False)
    try:
        yield
    finally:
        pm.undoInfo(stateWithoutFlush=True)

@contextlib.contextmanager
def disable_auto_keyframe():
    """
    Disable auto-keyframe within a with block.
    """
    original_auto_keyframe = pm.autoKeyframe(q=True, state=True)
    try:
        pm.autoKeyframe(state=False)
        yield
    finally:
        pm.autoKeyframe(state=original_auto_keyframe)

def load_plugin(plugin_name, required=True):
    """
    Load plugin_name, if available.  If required is true and the plugin isn't available, raise RuntimeError.

    pm.loadPlugin() is slow if called when a plugin is already loaded.  This checks if the
    plugin is loaded to avoid that.
    """
    if not pm.pluginInfo(plugin_name, q=True, loaded=True):
        try:
            pm.loadPlugin(plugin_name, quiet=True)
        except RuntimeError as e:
            pass

    if not pm.pluginInfo(plugin_name, q=True, registered=True):
        if required:
            raise RuntimeError('Plugin "%s" isn\'t available.' % plugin_name)
        return False
    return True

def copy_weights_to_skincluster(src_attr, skin_cluster, shape):
    src_indices = src_attr.getArrayIndices()
    if not src_indices:
        log.warning('Input has no deformer weights: %s', src_attr)
        return False

    src_values = src_attr.get()
    src_index_count = max(src_indices)+1
    
    weights_array = om.MDoubleArray(src_index_count)

    # Initialize all weights to 1, which is the default mask weight for values not
    # in the array.
    for index in xrange(src_index_count):
        weights_array[index] = 1

    for index, value in zip(src_indices, src_values):
        weights_array[index] = value

    skin_cluster.setWeights(shape, [0], weights_array, False)

    return True

def lock_attr(attr, lock='lock'):
    """
    If lock is 'lock', lock attr and hide it in the CB.  This is for removing attributes
    like translation on control nodes, where we don't want it cluttering the channel box.

    If lock is "lock_visible", lock attr, but leave it in the CB.  This is for attributes
    we don't want modified, but which are on nodes that aren't normally selected by the
    user, like alignment nodes.  Leaving these in the channel box is convenient if they
    need to be unlocked later, since the AE UI for doing this is cumbersome.

    If lock is 'hide', hide it in the CB and make it unkeyable, but don't lock it.
    We do this with the transform of control nodes which are visible in the viewport
    but whose position doesn't matter, so you can still move them around and put them
    where you want, without cluttering the CB.

    If lock is "unkeyable", make it unkeyable but leave it in the CB.  This is for
    internal nodes where the property is meaningful and which we need unlocked, but
    that shouldn't be keyed by the user.

    It's important to mark nodes under the Rig hierarchy as unkeyable or locked
    if they're not user controls.  This prevents them from being added to character
    sets and auto-keyed, which removes a huge amount of clutter.  It also prevents
    accidental bad changes, such as using "preserve children" and accidentally moving
    an alignment node when you think you're moving a control.
    """
    if lock == 'lock':
        pm.setAttr(attr, lock=True, cb=False, keyable=False)
    elif lock == 'lock_visible':
        pm.setAttr(attr, lock=True, cb=True, keyable=False)
    elif lock == 'hide':
        pm.setAttr(attr, lock=False, cb=False, keyable=False)
    elif lock == 'unkeyable':
        pm.setAttr(attr, lock=False, cb=True, keyable=False)
    elif lock == 'keyable':
        pm.setAttr(attr, lock=False, cb=False, keyable=True)
    else:
        raise RuntimeError('Invalid lock state: %s' % lock)

def lock_translate(node, lock='lock'):
    for attr in ('translateX', 'translateY', 'translateZ'):
        try:
            lock_attr(node.attr(attr), lock=lock)
        except pm.MayaAttributeError:
            pass

def lock_rotate(node, lock='lock'):
    for attr in ('rotateX', 'rotateY', 'rotateZ'):
        try:
            lock_attr(node.attr(attr), lock=lock)
        except pm.MayaAttributeError:
            pass

def lock_scale(node, lock='lock'):
    for attr in ('scaleX', 'scaleY', 'scaleZ'):
        try:
            lock_attr(node.attr(attr), lock=lock)
        except pm.MayaAttributeError:
            pass

def lock_trs(node, lock='lock'):
    lock_translate(node, lock=lock)
    lock_rotate(node, lock=lock)
    lock_scale(node, lock=lock)

def create_attribute_proxy(node, attr):
    """
    Create a proxy for attr on node.  Return the new attribute.
    """
    # Be sure to assign both the long and short name.  Being inconsistent with the
    # original attribute can lead to issues with the CB.
    short_name = pm.attributeQuery(attr.attrName(), node=attr.node(), shortName=True)
    long_name = pm.attributeQuery(attr.attrName(), node=attr.node(), longName=True)
    pm.addAttr(node, ln=long_name, sn=short_name, proxy=attr)
    return node.attr(long_name)
    
def add_attr(nodes, name, *args, **kwargs):
    # Add the attribute to the first node.
    pm.addAttr(nodes[0], ln=name, *args, **kwargs)
    attr = nodes[0].attr(name)

    # If there's more than one node, add proxies to the rest.
    for node in nodes[1:]:
        create_attribute_proxy(node, attr)

    return attr

from pymel.tools.py2mel import py2melProc as origPy2melProc
def py2melProc(returnType='', procName=None, argTypes=None):
    """
    A wrapper t make pymel's py2melProc work properly as a decorator.
    
    Note that py2melProc is only partially implemented and can only be used for very
    simple cases.
    """
    def wrapper(function):
        function.mel_proc_name = origPy2melProc(function, returnType=returnType, procName=procName, argTypes=argTypes)
        return function
    return wrapper

# This set of helpers allows temporarily setting attributes, optionVars, etc., and
# restoring them to their original state later.
class SetAndRestore(object):
    def __init__(self, value=None):
        self.old_value = self.get()
        if value is not None:
            self.set(value)

    def restore(self):
        self.set(self.old_value)

class SetAndRestoreAttr(SetAndRestore):
    ConnectionWrapper = namedtuple('ConnectionWrapper', ['connection', 'value'])
            
    def __init__(self, attr, value=None, optional=False):
        self.attr = pm.PyNode(attr)
        self.optional = optional
        super(SetAndRestoreAttr, self).__init__(value)

    def __str__(self):
        return 'SetAndRestoreAttr(%s)' % self.attr

    def get(self):
        try:
            # See if this node is connected and needs to be disconnected to set the value.
            connections = self.attr.listConnections(s=True, d=False, p=True)
            assert len(connections) < 2 # can't have multiple inputs
            if connections:
                connection = connections[0]
                connection.disconnect(self.attr)
            else:
                connection = None

            # Get the value.
            value = self.attr.get()

            # Work around weirdness with some string attributes, like defaultRenderGlobals.preRenderMel.
            # These return None if they've never been set before, and if we set it to something, we
            # have no way to restore the original value, so restore the empty string instead.
            if value is None:
                attr_type = self.attr.get(type=True)

                # I've only seen this with strings.
                assert attr_type == 'string'

                value = ''

            # Include the original connection in the value, if any, so we can restore it later.
            # Wrap this in a small helper class, so we can distinguish it.
            return self.ConnectionWrapper(connection, value)
        except (pm.MayaNodeError, pm.MayaAttributeError):
            if self.optional:
                return None
            raise

    def set(self, value):
        try:
            connection = None

            if isinstance(value, self.ConnectionWrapper):
                # If value is a ConnectionWrapper, it contains both the value and an optional connection
                # to restore.  This is what we use in this class to restore values.
                connection = value.connection
                value = value.value
            elif isinstance(value, pm.general.Attribute):
                # If this is an attribute, connect to it.
                connection = value
                value = None
            else:
                # Otherwise, this is the value to set.
                pass

            # Restore the value.
            if value is not None:
                pm.setAttr(self.attr, value)

            if connection is not None:
                connection.connect(self.attr)
        except (pm.MayaNodeError, pm.MayaAttributeError):
            if self.optional:
                return
            raise

class SetAndRestoreOptionVar(SetAndRestore):
    def __init__(self, var, value=None):
        self.var = var
        super(SetAndRestoreOptionVar, self).__init__(value)

    def __str__(self):
        return 'SetAndRestoreOptionVar(%s)' % self.var

    def get(self):
        if not pm.optionVar(exists=self.var):
            return None
        return pm.optionVar(q=self.var)

    def set(self, value):
        if value is None:
            pm.optionVar(remove=self.var)
        else:
            pm.optionVar(sv=(self.var, value))

class SetAndRestoreCmd(SetAndRestore):
    def __init__(self, cmd, key, value=None):
        self.cmd = cmd
        self.key = key

        super(SetAndRestoreCmd, self).__init__(value)

    def __str__(self):
        return 'SetAndRestoreCmd(%s)' % self.cmd

    def get(self):
        args = { 'q': True }
        if self.key is not None:
            args[self.key] = True
        result = self.cmd(**args)

        # Grr.  Why does this return an array?
        if self.cmd is pm.renderSetupLocalOverride:
            result = result[0]
        return result
    
    def set(self, value):
        args = []
        kwargs = {}
        if self.key is not None:
            # eg. pm.ogs(pause=True)
            kwargs[self.key] = value
        else:
            # eg. pm.currentTime(10)
            args.append(value)
        self.cmd(*args, **kwargs)

class SetAndRestorePauseViewport(SetAndRestoreCmd):
    """
    Pause and unpause the viewport.
    """
    def __init__(self, value=None):
        super(SetAndRestorePauseViewport, self).__init__(pm.ogs, 'pause', value)
        
    def set(self, value):
        # Work around ogs.pause being inconsistent with other Maya commands: instead of
        # ogs -pause 1 pausing and -pause 0 unpausing, -pause 1 toggles the current value.
        if self.get() != value:
            super(SetAndRestorePauseViewport, self).set(True)

@contextlib.contextmanager
def restores(name='undo_on_exception'):
    """
    Run a block of code, restoring a list of scene changes at the end.
    """
    try:
        restores = []
        yield restores
    finally:
        # Restore changes.
        for restore in reversed(restores):
            restore.restore()

class _FilterMFnWarnings(object):
    def filter(self, record):
        return 'Could not create desired MFn' not in record.msg

def quiet_pymel_warnings(func):
    """
    PyMel prints a lot of spurious warnings like this when accessing nodes:
        
    Warning: pymel.core.general : Could not create desired MFn. Defaulting to MFnDependencyNode.
    
    This obscures actual warnings.  This wrapper temporarily silences this warning.
    The logger will be returned to normal when we return.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        pymel_logger = logging.getLogger('pymel.core.general')
        temp_filter = _FilterMFnWarnings()
        try:
            pymel_logger.addFilter(temp_filter)
            return func(*args, **kwargs)
        finally:
            pymel_logger.removeFilter(temp_filter)
    return wrapper

def create_or_replace_runtime_command(name, *args, **kwargs):
    """
    A wrapper around pm.runTimeCommand that deletes the command if it already exists
    before running the command.
    """
    # Delete the command if it already exists.
    if pm.runTimeCommand(name, exists=True):
        pm.runTimeCommand(name, e=True, delete=True)
    pm.runTimeCommand(name, *args, **kwargs)
      

def scene_framerate():
    """
    Return the scene framerate.
    """
    # Why does currentUnit return strings?  Nobody wants to be told "ntsc" when they ask for the framerate.
    return pm.mel.eval('getCadenceLineWorkingUnitInFPS')
