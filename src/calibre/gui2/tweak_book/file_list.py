#!/usr/bin/env python
# License: GPLv3 Copyright: 2013, Kovid Goyal <kovid at kovidgoyal.net>


import os
import posixpath
import sys
import textwrap
from collections import Counter, OrderedDict, defaultdict
from functools import lru_cache, partial

from qt.core import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFont,
    QFormLayout,
    QGridLayout,
    QIcon,
    QInputDialog,
    QItemSelectionModel,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPainter,
    QPixmap,
    QRadioButton,
    QScrollArea,
    QSize,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    Qt,
    QTimer,
    QTreeView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    pyqtSignal,
    sip,
)

from calibre import human_readable, sanitize_file_name
from calibre.ebooks.oeb.base import OEB_DOCS, OEB_STYLES
from calibre.ebooks.oeb.polish.cover import get_cover_page_name, get_raster_cover_name, is_raster_image
from calibre.ebooks.oeb.polish.css import add_stylesheet_links
from calibre.ebooks.oeb.polish.replace import get_recommended_folders, get_spine_order_for_all_files
from calibre.ebooks.oeb.polish.utils import OEB_FONTS, guess_type
from calibre.gui2 import choose_dir, choose_files, choose_save_file, elided_text, error_dialog, make_view_use_window_background, question_dialog
from calibre.gui2.tweak_book import CONTAINER_DND_MIMETYPE, current_container, editors, tprefs
from calibre.gui2.tweak_book.editor import syntax_from_mime
from calibre.gui2.tweak_book.templates import template_for
from calibre.startup import connect_lambda
from calibre.utils.fonts.utils import get_font_names
from calibre.utils.icu import numeric_sort_key
from calibre.utils.localization import ngettext, pgettext
from calibre_extensions.progress_indicator import set_no_activate_on_click
from polyglot.binary import as_hex_unicode
from polyglot.builtins import iteritems

FILE_COPY_MIME = 'application/calibre-edit-book-files'
TOP_ICON_SIZE = 24
NAME_ROLE = Qt.ItemDataRole.UserRole
CATEGORY_ROLE = NAME_ROLE + 1
LINEAR_ROLE = CATEGORY_ROLE + 1
MIME_ROLE = LINEAR_ROLE + 1
TEMP_NAME_ROLE = MIME_ROLE + 1
NBSP = '\xa0'


@lru_cache(maxsize=2)
def category_defs():
    return (
        ('text', _('Text'), _('Chapter-')),
        ('styles', _('Styles'), _('Style-')),
        ('images', _('Images'), _('Image-')),
        ('fonts', _('Fonts'), _('Font-')),
        ('misc', pgettext('edit book file type', 'Miscellaneous'), _('Misc-')),
    )


def name_is_ok(name, show_error):
    if not name or not name.strip():
        return show_error('') and False
    ext = name.rpartition('.')[-1]
    if not ext or ext == name:
        return show_error(_('The file name must have an extension')) and False
    norm = name.replace('\\', '/')
    parts = name.split('/')
    for x in parts:
        if sanitize_file_name(x) != x:
            return show_error(_('The file name contains invalid characters')) and False
    if current_container().has_name(norm):
        return show_error(_('This file name already exists in the book')) and False
    show_error('')
    return True


def get_bulk_rename_settings(parent, number, msg=None, sanitize=sanitize_file_name,
        leading_zeros=True, prefix=None, category='text', allow_spine_order=False):  # {{{
    d = QDialog(parent)
    d.setWindowTitle(_('Bulk rename items'))
    d.l = l = QFormLayout(d)
    d.setLayout(l)
    d.prefix = p = QLineEdit(d)
    default_prefix = {k:v for k, __, v in category_defs()}.get(category, _('Chapter-'))
    previous = tprefs.get('file-list-bulk-rename-prefix', {})
    prefix = prefix or previous.get(category, default_prefix)
    p.setText(prefix)
    p.selectAll()
    d.la = la = QLabel(msg or _(
        'All selected files will be renamed to the form prefix-number'))
    l.addRow(la)
    l.addRow(_('&Prefix:'), p)
    d.num = num = QSpinBox(d)
    num.setMinimum(0), num.setValue(1), num.setMaximum(10000)
    l.addRow(_('Starting &number:'), num)
    if allow_spine_order:
        d.spine_order = QCheckBox(_('Rename files according to their book order'))
        d.spine_order.setToolTip(textwrap.fill(_(
            'Rename the selected files according to the order they appear in the book, instead of the order they were selected in.')))
        l.addRow(d.spine_order)
    d.bb = bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    bb.accepted.connect(d.accept), bb.rejected.connect(d.reject)
    l.addRow(bb)
    ans = {'prefix': None, 'start': None}

    if d.exec() == QDialog.DialogCode.Accepted:
        prefix = sanitize(str(d.prefix.text()))
        previous[category] = prefix
        tprefs.set('file-list-bulk-rename-prefix', previous)
        num = d.num.value()
        fmt = '%d'
        if leading_zeros:
            largest = num + number - 1
            fmt = f'%0{len(str(largest))}d'
        ans['prefix'] = prefix + fmt
        ans['start'] = num
        if allow_spine_order:
            ans['spine_order'] = d.spine_order.isChecked()
    return ans
# }}}


class ItemDelegate(QStyledItemDelegate):  # {{{

    rename_requested = pyqtSignal(object, object, object)

    def setEditorData(self, editor, index):
        name = str(index.data(NAME_ROLE) or '')
        # We do this because Qt calls selectAll() unconditionally on the
        # editor, and we want only a part of the file name to be selected
        QTimer.singleShot(0, partial(self.set_editor_data, name, editor))

    def set_editor_data(self, name, editor):
        if sip.isdeleted(editor):
            return
        editor.setText(name)
        ext_pos = name.rfind('.')
        slash_pos = name.rfind('/')
        if slash_pos == -1 and ext_pos > 0:
            editor.setSelection(0, ext_pos)
        elif ext_pos > -1 and slash_pos > -1 and ext_pos > slash_pos + 1:
            editor.setSelection(slash_pos+1, ext_pos - slash_pos - 1)
        else:
            editor.selectAll()

    def setModelData(self, editor, model, index):
        newname = str(editor.text())
        oldname = str(index.data(NAME_ROLE) or '')
        if newname != oldname:
            self.rename_requested.emit(index, oldname, newname)

    def sizeHint(self, option, index):
        ans = QStyledItemDelegate.sizeHint(self, option, index)
        top_level = not index.parent().isValid()
        ans += QSize(0, 20 if top_level else 10)
        return ans

    def paint(self, painter, option, index):
        top_level = not index.parent().isValid()
        hover = option.state & QStyle.StateFlag.State_MouseOver
        cc = current_container()

        def safe_size(index):
            try:
                return cc.filesize(str(index.data(NAME_ROLE) or ''))
            except OSError:
                return 0

        if hover:
            if top_level:
                m = index.model()
                count = m.rowCount(index)
                total_size = human_readable(sum(safe_size(m.index(r, 0, index)) for r in range(count)))
                suffix = f'{NBSP}{count}@{total_size}'
            else:
                suffix = NBSP + human_readable(safe_size(index))
            br = painter.boundingRect(option.rect, Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter, suffix)
        if top_level and index.row() > 0:
            option.rect.adjust(0, 5, 0, 0)
            painter.drawLine(option.rect.topLeft(), option.rect.topRight())
            option.rect.adjust(0, 1, 0, 0)
        if hover:
            option.rect.adjust(0, 0, -br.width(), 0)
        QStyledItemDelegate.paint(self, painter, option, index)
        if hover:
            option.rect.adjust(0, 0, br.width(), 0)
            painter.drawText(option.rect, Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter, suffix)
# }}}


class OpenWithHandler:  # {{{

    def add_open_with_actions(self, menu, file_name):
        from calibre.gui2.open_with import edit_programs, populate_menu
        fmt = file_name.rpartition('.')[-1].lower()
        if not fmt:
            return
        m = QMenu(_('Open %s with...') % file_name)

        def connect_action(ac, entry):
            connect_lambda(ac.triggered, self, lambda self: self.open_with(file_name, fmt, entry))

        populate_menu(m, connect_action, fmt)
        if len(m.actions()) == 0:
            menu.addAction(_('Open %s with...') % file_name, partial(self.choose_open_with, file_name, fmt))
        else:
            m.addSeparator()
            m.addAction(_('Add other application for %s files...') % fmt.upper(), partial(self.choose_open_with, file_name, fmt))
            m.addAction(_('Edit Open with applications...'), partial(edit_programs, fmt, self))
            menu.addMenu(m)
            menu.ow = m

    def choose_open_with(self, file_name, fmt):
        from calibre.gui2.open_with import choose_program
        entry = choose_program(fmt, self)
        if entry is not None:
            self.open_with(file_name, fmt, entry)

    def open_with(self, file_name, fmt, entry):
        raise NotImplementedError()
# }}}


class FileList(QTreeWidget, OpenWithHandler):

    delete_requested = pyqtSignal(object, object)
    reorder_spine = pyqtSignal(object)
    rename_requested = pyqtSignal(object, object)
    bulk_rename_requested = pyqtSignal(object)
    edit_file = pyqtSignal(object, object, object)
    merge_requested = pyqtSignal(object, object, object)
    mark_requested = pyqtSignal(object, object)
    export_requested = pyqtSignal(object, object)
    replace_requested = pyqtSignal(object, object, object, object)
    link_stylesheets_requested = pyqtSignal(object, object, object)
    initiate_file_copy = pyqtSignal(object)
    initiate_file_paste = pyqtSignal()
    open_file_with = pyqtSignal(object, object, object)

    def __init__(self, parent=None):
        QTreeWidget.__init__(self, parent)
        self.pending_renames = {}
        make_view_use_window_background(self)
        self.categories = {}
        self.ordered_selected_indexes = False
        set_no_activate_on_click(self)
        self.current_edited_name = None
        self.delegate = ItemDelegate(self)
        self.delegate.rename_requested.connect(self.possible_rename_requested, type=Qt.ConnectionType.QueuedConnection)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.setItemDelegate(self.delegate)
        self.setIconSize(QSize(16, 16))
        self.header().close()
        self.setDragEnabled(True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setAutoScroll(True)
        self.setAutoScrollMargin(TOP_ICON_SIZE*2)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setAutoExpandDelay(1000)
        self.setAnimated(True)
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)
        self.root = self.invisibleRootItem()
        self.emblem_cache = {}
        self.rendered_emblem_cache = {}
        self.font_name_cache = {}
        self.top_level_pixmap_cache = {
            name: QIcon.ic(icon).pixmap(TOP_ICON_SIZE, TOP_ICON_SIZE)
            for name, icon in iteritems({
                'text':'keyboard-prefs.png',
                'styles':'lookfeel.png',
                'fonts':'font.png',
                'misc':'mimetypes/dir.png',
                'images':'view-image.png',
            })}
        self.itemActivated.connect(self.item_double_clicked)

    def possible_rename_requested(self, index, old, new):
        if old != new:
            self.pending_renames[old] = new
            QTimer.singleShot(10, self.dispatch_pending_renames)
            item = self.itemFromIndex(index)
            item.setData(0, TEMP_NAME_ROLE, item.text(0))
            item.setText(0, new)

    def restore_temp_names(self):
        for item in self.all_files:
            q = item.data(0, TEMP_NAME_ROLE)
            if q:
                item.setText(0, q)
                item.setData(0, TEMP_NAME_ROLE, None)

    def dispatch_pending_renames(self):
        if self.pending_renames:
            if self.state() != QAbstractItemView.State.EditingState:
                pr, self.pending_renames = self.pending_renames, {}
                if len(pr) == 1:
                    old, new = tuple(pr.items())[0]
                    self.rename_requested.emit(old, new)
                else:
                    ur = {}
                    seen_vals = {c.data(0, NAME_ROLE) or '' for c in self.all_files}
                    for k, v in pr.items():
                        if v not in seen_vals:
                            seen_vals.add(v)
                            ur[k] = v
                    self.bulk_rename_requested.emit(ur)
            else:
                QTimer.singleShot(10, self.dispatch_pending_renames)

    def mimeTypes(self):
        ans = QTreeWidget.mimeTypes(self)
        ans.append(CONTAINER_DND_MIMETYPE)
        return ans

    def mimeData(self, indices):
        ans = QTreeWidget.mimeData(self, indices)
        names = (idx.data(0, NAME_ROLE) for idx in indices if idx.data(0, MIME_ROLE))
        ans.setData(CONTAINER_DND_MIMETYPE, '\n'.join(filter(None, names)).encode('utf-8'))
        return ans

    def dropMimeData(self, parent, index, data, action):
        if not parent or not data.hasFormat(CONTAINER_DND_MIMETYPE):
            return False
        names = bytes(data.data(CONTAINER_DND_MIMETYPE)).decode('utf-8').splitlines()
        if not names:
            return False
        category = parent.data(0, CATEGORY_ROLE)
        if category is None:
            self.handle_reorder_drop(parent, index, names)
        elif category == 'text':
            self.handle_merge_drop(parent, names)
        return False  # we have to return false to prevent Qt's internal machinery from re-ordering nodes

    def handle_merge_drop(self, target_node, names):
        category_node = target_node.parent()
        current_order = {category_node.child(i).data(0, NAME_ROLE):i for i in range(category_node.childCount())}
        names = sorted(names, key=lambda x: current_order.get(x, -1))
        target_name = target_node.data(0, NAME_ROLE)
        if len(names) == 1:
            msg = _('Merge the file {0} into the file {1}?').format(elided_text(names[0]), elided_text(target_name))
        else:
            msg = _('Merge the {0} selected files into the file {1}?').format(len(names), elided_text(target_name))
        if question_dialog(self, _('Merge files'), msg, skip_dialog_name='edit-book-merge-on-drop'):
            names.append(target_name)
            names = sorted(names, key=lambda x: current_order.get(x, -1))
            self.merge_requested.emit(target_node.data(0, CATEGORY_ROLE), names, target_name)

    def handle_reorder_drop(self, category_node, idx, names):
        current_order = tuple(category_node.child(i).data(0, NAME_ROLE) for i in range(category_node.childCount()))
        linear_map = {category_node.child(i).data(0, NAME_ROLE):category_node.child(i).data(0, LINEAR_ROLE) for i in range(category_node.childCount())}
        order_map = {name: i for i, name in enumerate(current_order)}
        try:
            insert_before = current_order[idx]
        except IndexError:
            insert_before = None
        names = sorted(names, key=lambda x: order_map.get(x, -1))
        moved_names = frozenset(names)
        new_names = [n for n in current_order if n not in moved_names]
        try:
            insertion_point = len(new_names) if insert_before is None else new_names.index(insert_before)
        except ValueError:
            return
        new_names = new_names[:insertion_point] + names + new_names[insertion_point:]
        order = [[name, linear_map[name]] for name in new_names]
        self.request_reorder(order)

    def request_reorder(self, order):
        # Ensure that all non-linear items are at the end, by making any non-linear
        # items not at the end, linear
        for i, (name, linear) in tuple(enumerate(order)):
            if not linear and i < len(order) - 1 and order[i+1][1]:
                order[i][1] = True
        self.reorder_spine.emit(order)

    def dropEvent(self, event):
        # the dropEvent() implementation of QTreeWidget handles InternalMoves
        # internally and is not suitable for us. QTreeView::dropEvent calls
        # dropMimeData() where we handle the drop
        QTreeView.dropEvent(self, event)

    @property
    def current_name(self):
        ci = self.currentItem()
        if ci is not None:
            return str(ci.data(0, NAME_ROLE) or '')
        return ''

    def get_state(self):
        s = {'pos':self.verticalScrollBar().value()}
        s['expanded'] = {c for c, item in iteritems(self.categories) if item.isExpanded()}
        s['selected'] = {str(i.data(0, NAME_ROLE) or '') for i in self.selectedItems()}
        return s

    def set_state(self, state):
        for category, item in iteritems(self.categories):
            item.setExpanded(category in state['expanded'])
        self.verticalScrollBar().setValue(state['pos'])
        for parent in self.categories.values():
            for c in (parent.child(i) for i in range(parent.childCount())):
                name = str(c.data(0, NAME_ROLE) or '')
                if name in state['selected']:
                    c.setSelected(True)

    def item_from_name(self, name):
        for parent in self.categories.values():
            for c in (parent.child(i) for i in range(parent.childCount())):
                q = str(c.data(0, NAME_ROLE) or '')
                if q == name:
                    return c

    def select_name(self, name, set_as_current_index=False):
        for c in self.all_files:
            q = str(c.data(0, NAME_ROLE) or '')
            c.setSelected(q == name)
            if q == name:
                self.scrollToItem(c)
                if set_as_current_index:
                    self.setCurrentItem(c)

    def select_names(self, names, current_name=None):
        for c in self.all_files:
            q = str(c.data(0, NAME_ROLE) or '')
            c.setSelected(q in names)
            if q == current_name:
                self.scrollToItem(c)
                s = self.selectionModel()
                s.setCurrentIndex(self.indexFromItem(c), QItemSelectionModel.SelectionFlag.NoUpdate)

    def mark_name_as_current(self, name):
        current = self.item_from_name(name)
        if current is not None:
            if self.current_edited_name is not None:
                ci = self.item_from_name(self.current_edited_name)
                if ci is not None:
                    ci.setData(0, Qt.ItemDataRole.FontRole, None)
            self.current_edited_name = name
            self.mark_item_as_current(current)

    def mark_item_as_current(self, item):
        font = QFont(self.font())
        font.setItalic(True)
        font.setBold(True)
        item.setData(0, Qt.ItemDataRole.FontRole, font)

    def clear_currently_edited_name(self):
        if self.current_edited_name:
            ci = self.item_from_name(self.current_edited_name)
            if ci is not None:
                ci.setData(0, Qt.ItemDataRole.FontRole, None)
        self.current_edited_name = None

    def build(self, container, preserve_state=True):
        if container is None:
            return
        if preserve_state:
            state = self.get_state()
        self.clear()
        self.root = self.invisibleRootItem()
        self.root.setFlags(Qt.ItemFlag.ItemIsDragEnabled)
        self.categories = {}
        for category, text, __ in category_defs():
            self.categories[category] = i = QTreeWidgetItem(self.root, 0)
            i.setText(0, text)
            i.setData(0, Qt.ItemDataRole.DecorationRole, self.top_level_pixmap_cache[category])
            f = i.font(0)
            f.setBold(True)
            i.setFont(0, f)
            i.setData(0, NAME_ROLE, category)
            flags = Qt.ItemFlag.ItemIsEnabled
            if category == 'text':
                flags |= Qt.ItemFlag.ItemIsDropEnabled
            i.setFlags(flags)

        processed, seen = {}, {}

        cover_page_name = get_cover_page_name(container)
        cover_image_name = get_raster_cover_name(container)
        manifested_names = set()
        for names in container.manifest_type_map.values():
            manifested_names |= set(names)

        def get_category(name, mt):
            category = 'misc'
            if mt.startswith('image/'):
                category = 'images'
            elif mt in OEB_FONTS:
                category = 'fonts'
            elif mt in OEB_STYLES:
                category = 'styles'
            elif mt in OEB_DOCS:
                category = 'text'
            ext = name.rpartition('.')[-1].lower()
            if ext in {'ttf', 'otf', 'woff', 'woff2'}:
                # Probably wrong mimetype in the OPF
                category = 'fonts'
            return category

        def set_display_name(name, item):
            if tprefs['file_list_shows_full_pathname']:
                text = name
            else:
                if name in processed:
                    # We have an exact duplicate (can happen if there are
                    # duplicates in the spine)
                    item.setText(0, processed[name].text(0))
                    item.setText(1, processed[name].text(1))
                    return

                parts = name.split('/')
                text = parts.pop()
                while text in seen and parts:
                    text = parts.pop() + '/' + text

            seen[text] = item
            item.setText(0, text)
            item.setText(1, as_hex_unicode(numeric_sort_key(text)))

        def render_emblems(item, emblems):
            emblems = tuple(emblems)
            if not emblems:
                return
            icon = self.rendered_emblem_cache.get(emblems, None)
            if icon is None:
                pixmaps = []
                for emblem in emblems:
                    pm = self.emblem_cache.get(emblem, None)
                    if pm is None:
                        pm = self.emblem_cache[emblem] = QIcon.ic(emblem).pixmap(self.iconSize())
                    pixmaps.append(pm)
                num = len(pixmaps)
                w, h = pixmaps[0].width(), pixmaps[0].height()
                if num == 1:
                    icon = self.rendered_emblem_cache[emblems] = QIcon(pixmaps[0])
                else:
                    canvas = QPixmap((num * w) + ((num-1)*2), h)
                    canvas.setDevicePixelRatio(pixmaps[0].devicePixelRatio())
                    canvas.fill(Qt.GlobalColor.transparent)
                    painter = QPainter(canvas)
                    for i, pm in enumerate(pixmaps):
                        painter.drawPixmap(int(i * (w + 2)/canvas.devicePixelRatio()), 0, pm)
                    painter.end()
                    icon = self.rendered_emblem_cache[emblems] = canvas
            item.setData(0, Qt.ItemDataRole.DecorationRole, icon)

        cannot_be_renamed = container.names_that_must_not_be_changed
        ncx_mime = guess_type('a.ncx')
        nav_items = frozenset(container.manifest_items_with_property('nav'))

        def create_item(name, linear=None):
            imt = container.mime_map.get(name, guess_type(name))
            icat = get_category(name, imt)
            category = 'text' if linear is not None else ({'text':'misc'}.get(icat, icat))
            item = QTreeWidgetItem(self.categories['text' if linear is not None else category], 1)
            flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            if category == 'text':
                flags |= Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled
            if name not in cannot_be_renamed:
                flags |= Qt.ItemFlag.ItemIsEditable
            item.setFlags(flags)
            item.setStatusTip(0, _('Full path: ') + name)
            item.setData(0, NAME_ROLE, name)
            item.setData(0, CATEGORY_ROLE, category)
            item.setData(0, LINEAR_ROLE, bool(linear))
            item.setData(0, MIME_ROLE, imt)

            set_display_name(name, item)
            tooltips = []
            emblems = []
            if name in {cover_page_name, cover_image_name}:
                emblems.append('default_cover.png')
                tooltips.append(_('This file is the cover %s for this book') % (_('image') if name == cover_image_name else _('page')))
            if name in container.opf_name:
                emblems.append('metadata.png')
                tooltips.append(_('This file contains all the metadata and book structure information'))
            if imt == ncx_mime or name in nav_items:
                emblems.append('toc.png')
                tooltips.append(_('This file contains the metadata table of contents'))
            if name not in manifested_names and not container.ok_to_be_unmanifested(name):
                emblems.append('dialog_question.png')
                tooltips.append(_('This file is not listed in the book manifest'))
            if linear is False:
                emblems.append('arrow-down.png')
                tooltips.append(_('This file is marked as non-linear in the spine\nDrag it to the top to make it linear'))
            if linear is None and icat == 'text':
                # Text item outside spine
                emblems.append('dialog_warning.png')
                tooltips.append(_('This file is a text file that is not referenced in the spine'))
            if category == 'text' and name in processed:
                # Duplicate entry in spine
                emblems.append('dialog_error.png')
                tooltips.append(_('This file occurs more than once in the spine'))
            if category == 'fonts' and name.rpartition('.')[-1].lower() in ('ttf', 'otf'):
                fname = self.get_font_family_name(name)
                if fname:
                    tooltips.append(fname)
                else:
                    emblems.append('dialog_error.png')
                    tooltips.append(_('Not a valid font'))

            render_emblems(item, emblems)
            if tooltips:
                item.setData(0, Qt.ItemDataRole.ToolTipRole, '\n'.join(tooltips))
            return item

        for name, linear in container.spine_names:
            processed[name] = create_item(name, linear=linear)

        for name in container.name_path_map:
            if name in processed:
                continue
            processed[name] = create_item(name)

        for name, c in iteritems(self.categories):
            c.setExpanded(True)
            if name != 'text':
                c.sortChildren(1, Qt.SortOrder.AscendingOrder)

        if preserve_state:
            self.set_state(state)

        if self.current_edited_name:
            item = self.item_from_name(self.current_edited_name)
            if item is not None:
                self.mark_item_as_current(item)

    def get_font_family_name(self, name):
        try:
            with current_container().open(name) as f:
                f.seek(0, os.SEEK_END)
                sz = f.tell()
        except Exception:
            sz = 0
        key = name, sz
        if key not in self.font_name_cache:
            raw = current_container().raw_data(name, decode=False)
            try:
                ans = get_font_names(raw)[-1]
            except Exception:
                ans = None
            self.font_name_cache[key] = ans
        return self.font_name_cache[key]

    def select_all_in_category(self, cname):
        parent = self.categories[cname]
        for c in (parent.child(i) for i in range(parent.childCount())):
            c.setSelected(True)

    def deselect_all_in_category(self, cname):
        parent = self.categories[cname]
        for c in (parent.child(i) for i in range(parent.childCount())):
            c.setSelected(False)

    def show_context_menu(self, point):
        item = self.itemAt(point)
        if item is None:
            return
        if item in self.categories.values():
            m = self.build_category_context_menu(item)
        else:
            m = self.build_item_context_menu(item)
        if m is not None and len(list(m.actions())) > 0:
            m.popup(self.mapToGlobal(point))

    def build_category_context_menu(self, item):
        m = QMenu(self)
        cn = str(item.data(0, NAME_ROLE) or '')
        if cn:
            name = item.data(0, Qt.DisplayRole)
            m.addAction(_('Select all {} files').format(name), partial(self.select_all_in_category, cn))
            m.addAction(_('De-select all {} files').format(name), partial(self.deselect_all_in_category, cn))
        return m

    def build_item_context_menu(self, item):
        m = QMenu(self)
        sel = self.selectedItems()
        num = len(sel)
        container = current_container()
        ci = self.currentItem()
        if ci is not None:
            cn = str(ci.data(0, NAME_ROLE) or '')
            mt = str(ci.data(0, MIME_ROLE) or '')
            cat = str(ci.data(0, CATEGORY_ROLE) or '')
            n = elided_text(cn.rpartition('/')[-1])
            m.addAction(QIcon.ic('save.png'), _('Export %s') % n, partial(self.export, cn))
            if cn not in container.names_that_must_not_be_changed and cn not in container.names_that_must_not_be_removed and mt not in OEB_FONTS:
                m.addAction(_('Replace %s with file...') % n, partial(self.replace, cn))
            if num > 1:
                m.addAction(QIcon.ic('save.png'), _('Export all %d selected files') % num, self.export_selected)
            if cn not in container.names_that_must_not_be_changed:
                self.add_open_with_actions(m, cn)

            m.addSeparator()

            m.addAction(QIcon.ic('modified.png'), _('&Rename %s') % n, self.edit_current_item)
            if is_raster_image(mt):
                m.addAction(QIcon.ic('default_cover.png'), _('Mark %s as cover image') % n, partial(self.mark_as_cover, cn))
            elif current_container().SUPPORTS_TITLEPAGES and mt in OEB_DOCS and cat == 'text':
                m.addAction(QIcon.ic('default_cover.png'), _('Mark %s as cover page') % n, partial(self.mark_as_titlepage, cn))
            if mt in OEB_DOCS and cat in ('text', 'misc') and current_container().opf_version_parsed.major > 2:
                m.addAction(QIcon.ic('toc.png'), _('Mark %s as Table of Contents') % n, partial(self.mark_as_nav, cn))
            m.addSeparator()

        if num > 0:
            m.addSeparator()
            if num > 1:
                m.addAction(QIcon.ic('modified.png'), _('&Bulk rename the selected files'), self.request_bulk_rename)
            m.addAction(QIcon.ic('modified.png'), _('Change the file extensions for the selected files'), self.request_change_ext)
            m.addAction(QIcon.ic('trash.png'), ngettext(
                '&Delete the selected file', '&Delete the {} selected files', num).format(num), self.request_delete)
            m.addAction(QIcon.ic('edit-copy.png'), ngettext(
                '&Copy the selected file to another editor instance',
                '&Copy the {} selected files to another editor instance', num).format(num), self.copy_selected_files)
            m.addSeparator()
        md = QApplication.instance().clipboard().mimeData()
        if md.hasUrls() and md.hasFormat(FILE_COPY_MIME):
            import json
            name_map = json.loads(bytes(md.data(FILE_COPY_MIME)))
            m.addAction(ngettext(
                _('Paste file from other editor instance'),
                _('Paste {} files from other editor instance'),
                len(name_map)).format(len(name_map)), self.paste_from_other_instance)

        selected_map = defaultdict(list)
        for item in sel:
            selected_map[str(item.data(0, CATEGORY_ROLE) or '')].append(str(item.data(0, NAME_ROLE) or ''))

        for items in selected_map.values():
            items.sort(key=self.index_of_name)

        if selected_map['text']:
            m.addAction(QIcon.ic('format-text-color.png'), _('Link &stylesheets...'), partial(self.link_stylesheets, selected_map['text']))

        if len(selected_map['text']) > 1:
            m.addAction(QIcon.ic('merge.png'), _('&Merge selected text files'), partial(self.start_merge, 'text', selected_map['text']))
        if len(selected_map['styles']) > 1:
            m.addAction(QIcon.ic('merge.png'), _('&Merge selected style files'), partial(self.start_merge, 'styles', selected_map['styles']))
        return m

    def choose_open_with(self, file_name, fmt):
        from calibre.gui2.open_with import choose_program
        entry = choose_program(fmt, self)
        if entry is not None:
            self.open_with(file_name, fmt, entry)

    def open_with(self, file_name, fmt, entry):
        self.open_file_with.emit(file_name, fmt, entry)

    def index_of_name(self, name):
        for category, parent in iteritems(self.categories):
            for i in range(parent.childCount()):
                item = parent.child(i)
                if str(item.data(0, NAME_ROLE) or '') == name:
                    return category, i
        return (None, -1)

    def merge_files(self):
        sel = self.selectedItems()
        selected_map = defaultdict(list)
        for item in sel:
            selected_map[str(item.data(0, CATEGORY_ROLE) or '')].append(str(item.data(0, NAME_ROLE) or ''))

        for items in selected_map.values():
            items.sort(key=self.index_of_name)
        if len(selected_map['text']) > 1:
            self.start_merge('text', selected_map['text'])
        elif len(selected_map['styles']) > 1:
            self.start_merge('styles', selected_map['styles'])
        else:
            error_dialog(self, _('Cannot merge'), _(
                'No files selected. Select two or more HTML files or two or more CSS files in the Files browser before trying to merge'), show=True)

    def start_merge(self, category, names):
        d = MergeDialog(names, self)
        if d.exec() == QDialog.DialogCode.Accepted and d.ans:
            self.merge_requested.emit(category, names, d.ans)

    def edit_current_item(self):
        if not current_container().SUPPORTS_FILENAMES:
            error_dialog(self, _('Cannot rename'), _(
                '%s books do not support file renaming as they do not use file names'
                ' internally. The filenames you see are automatically generated from the'
                ' internal structures of the original file.') % current_container().book_type.upper(), show=True)
            return
        if self.currentItem() is not None:
            self.editItem(self.currentItem())

    def mark_as_cover(self, name):
        self.mark_requested.emit(name, 'cover')

    def mark_as_titlepage(self, name):
        first = str(self.categories['text'].child(0).data(0, NAME_ROLE) or '') == name
        move_to_start = False
        if not first:
            move_to_start = question_dialog(self, _('Not first item'), _(
                '%s is not the first text item. You should only mark the'
                ' first text item as cover. Do you want to make it the'
                ' first item?') % elided_text(name),
                skip_dialog_name='edit-book-mark-as-titlepage-move-confirm',
                skip_dialog_skip_precheck=False
            )
        self.mark_requested.emit(name, f'titlepage:{move_to_start!r}')

    def mark_as_nav(self, name):
        self.mark_requested.emit(name, 'nav')

    def keyPressEvent(self, ev):
        k = ev.key()
        mods = ev.modifiers() & (
            Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)
        if k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            ev.accept()
            self.request_delete()
        elif mods == (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            m = self.categories['text'].childCount()
            amt = {Qt.Key.Key_Up: -1, Qt.Key.Key_Down: 1, Qt.Key.Key_Home: -m, Qt.Key.Key_End: m}.get(k, None)
            if amt is not None:
                ev.accept()
                self.move_selected_text_items(amt)
            else:
                return QTreeWidget.keyPressEvent(self, ev)
        else:
            return QTreeWidget.keyPressEvent(self, ev)

    def request_rename_common(self):
        if not current_container().SUPPORTS_FILENAMES:
            error_dialog(self, _('Cannot rename'), _(
                '%s books do not support file renaming as they do not use file names'
                ' internally. The filenames you see are automatically generated from the'
                ' internal structures of the original file.') % current_container().book_type.upper(), show=True)
            return
        names = {str(item.data(0, NAME_ROLE) or '') for item in self.selectedItems()}
        bad = names & current_container().names_that_must_not_be_changed
        if bad:
            error_dialog(self, _('Cannot rename'),
                         _('The file(s) %s cannot be renamed.') % ('<b>{}</b>'.format(', '.join(bad))), show=True)
            return
        names = sorted(names, key=self.index_of_name)
        return names

    def request_bulk_rename(self):
        names = self.request_rename_common()
        if names is not None:
            categories = Counter(str(item.data(0, CATEGORY_ROLE) or '') for item in self.selectedItems())
            settings = get_bulk_rename_settings(self, len(names), category=categories.most_common(1)[0][0], allow_spine_order=True)
            fmt, num = settings['prefix'], settings['start']
            if fmt is not None:
                def change_name(name, num):
                    parts = name.split('/')
                    base, ext = parts[-1].rpartition('.')[0::2]
                    parts[-1] = (fmt % num) + '.' + ext
                    return '/'.join(parts)
                if settings['spine_order']:
                    order_map = get_spine_order_for_all_files(current_container())
                    select_map = {n:i for i, n in enumerate(names)}

                    def key(n):
                        return order_map.get(n, (sys.maxsize, select_map[n]))
                    name_map = {n: change_name(n, num + i) for i, n in enumerate(sorted(names, key=key))}
                else:
                    name_map = {n:change_name(n, num + i) for i, n in enumerate(names)}
                self.bulk_rename_requested.emit(name_map)

    def request_change_ext(self):
        names = self.request_rename_common()
        if names is not None:
            text, ok = QInputDialog.getText(self, _('Rename files'), _('New file extension:'))
            if ok and text:
                ext = text.lstrip('.')

                def change_name(name):
                    base = posixpath.splitext(name)[0]
                    return base + '.' + ext
                name_map = {n:change_name(n) for n in names}
                self.bulk_rename_requested.emit(name_map)

    @property
    def selected_names(self):
        ans = {str(item.data(0, NAME_ROLE) or '') for item in self.selectedItems()}
        ans.discard('')
        return ans

    @property
    def selected_names_in_order(self):
        root = self.invisibleRootItem()
        for category_item in (root.child(i) for i in range(root.childCount())):
            for child in (category_item.child(i) for i in range(category_item.childCount())):
                if child.isSelected():
                    name = child.data(0, NAME_ROLE)
                    if name:
                        yield name

    def move_selected_text_items(self, amt: int) -> bool:
        parent = self.categories['text']
        children = tuple(parent.child(i) for i in range(parent.childCount()))
        selected_names = tuple(c.data(0, NAME_ROLE) for c in children if c.isSelected())
        if not selected_names or amt == 0:
            return False
        current_order = tuple(c.data(0, NAME_ROLE) for c in children)
        linear_map = {c.data(0, NAME_ROLE):c.data(0, LINEAR_ROLE) for c in children}
        order_map = {name: i for i, name in enumerate(current_order)}
        new_order = list(current_order)
        changed = False
        items = reversed(selected_names) if amt > 0 else selected_names
        if amt < 0:
            items = selected_names
            delta = max(amt, -order_map[selected_names[0]])
        else:
            items = reversed(selected_names)
            delta = min(amt, len(children) - 1 - order_map[selected_names[-1]])
        for name in items:
            i = order_map[name]
            new_i = min(max(0, i + delta), len(current_order) - 1)
            if new_i != i:
                changed = True
                del new_order[i]
                new_order.insert(new_i, name)
        if changed:
            self.request_reorder([[n, linear_map[n]] for n in new_order])
        return changed

    def copy_selected_files(self):
        self.initiate_file_copy.emit(tuple(self.selected_names_in_order))

    def paste_from_other_instance(self):
        self.initiate_file_paste.emit()

    def request_delete(self):
        names = self.selected_names
        bad = names & current_container().names_that_must_not_be_removed
        if bad:
            return error_dialog(self, _('Cannot delete'),
                         _('The file(s) %s cannot be deleted.') % ('<b>{}</b>'.format(', '.join(bad))), show=True)

        text = self.categories['text']
        children = (text.child(i) for i in range(text.childCount()))
        spine_removals = [(str(item.data(0, NAME_ROLE) or ''), item.isSelected()) for item in children]
        other_removals = {str(item.data(0, NAME_ROLE) or '') for item in self.selectedItems()
                          if str(item.data(0, CATEGORY_ROLE) or '') != 'text'}
        self.delete_requested.emit(spine_removals, other_removals)

    def delete_done(self, spine_removals, other_removals):
        removals = []
        for i, (name, remove) in enumerate(spine_removals):
            if remove:
                removals.append(self.categories['text'].child(i))
        for category, parent in iteritems(self.categories):
            if category != 'text':
                for i in range(parent.childCount()):
                    child = parent.child(i)
                    if str(child.data(0, NAME_ROLE) or '') in other_removals:
                        removals.append(child)

        # The sorting by index is necessary otherwise Qt crashes with recursive
        # repaint detected message
        for c in sorted(removals, key=lambda x:x.parent().indexOfChild(x), reverse=True):
            sip.delete(c)

        # A bug in the raster paint engine on linux causes a crash if the scrollbar
        # is at the bottom and the delete happens to cause the scrollbar to
        # update
        b = self.verticalScrollBar()
        if b.value() == b.maximum():
            b.setValue(b.minimum())
            QTimer.singleShot(0, lambda: b.setValue(b.maximum()))

    def __enter__(self):
        self.ordered_selected_indexes = True

    def __exit__(self, *args):
        self.ordered_selected_indexes = False

    def selectedIndexes(self):
        ans = QTreeWidget.selectedIndexes(self)
        if self.ordered_selected_indexes:
            ans = list(sorted(ans, key=lambda idx:idx.row()))
        return ans

    def item_double_clicked(self, item, column):
        category = str(item.data(0, CATEGORY_ROLE) or '')
        if category:
            self._request_edit(item)

    def _request_edit(self, item):
        category = str(item.data(0, CATEGORY_ROLE) or '')
        mime = str(item.data(0, MIME_ROLE) or '')
        name = str(item.data(0, NAME_ROLE) or '')
        syntax = {'text':'html', 'styles':'css'}.get(category, None)
        self.edit_file.emit(name, syntax, mime)

    def request_edit(self, name):
        item = self.item_from_name(name)
        if item is not None:
            self._request_edit(item)
        else:
            error_dialog(self, _('Cannot edit'),
                         _('No item with the name %s was found') % name, show=True)

    def edit_next_file(self, currently_editing=None, backwards=False):
        category = self.categories['text']
        seen_current = False
        items = (category.child(i) for i in range(category.childCount()))
        if backwards:
            items = reversed(tuple(items))
        for item in items:
            name = str(item.data(0, NAME_ROLE) or '')
            if seen_current:
                self._request_edit(item)
                return True
            if currently_editing == name:
                seen_current = True
        return False

    @property
    def all_files(self):
        return (category.child(i) for category in self.categories.values() for i in range(category.childCount()))

    @property
    def searchable_names(self):
        ans = {'text':OrderedDict(), 'styles':OrderedDict(), 'selected':OrderedDict(), 'open':OrderedDict()}
        for item in self.all_files:
            category = str(item.data(0, CATEGORY_ROLE) or '')
            mime = str(item.data(0, MIME_ROLE) or '')
            name = str(item.data(0, NAME_ROLE) or '')
            ok = category in {'text', 'styles'}
            if ok:
                ans[category][name] = syntax_from_mime(name, mime)
            if not ok:
                if category == 'misc':
                    ok = mime in {guess_type('a.'+x) for x in ('opf', 'ncx', 'txt', 'xml')}
                elif category == 'images':
                    ok = mime == guess_type('a.svg')
            if ok:
                cats = []
                if item.isSelected():
                    cats.append('selected')
                if name in editors:
                    cats.append('open')
                for cat in cats:
                    ans[cat][name] = syntax_from_mime(name, mime)
        return ans

    def export(self, name):
        path = choose_save_file(self, 'tweak_book_export_file', _('Choose location'), filters=[
            (_('Files'), [name.rpartition('.')[-1].lower()])], all_files=False, initial_filename=name.split('/')[-1])
        if path:
            self.export_requested.emit(name, path)

    def export_selected(self):
        names = self.selected_names
        if not names:
            return
        path = choose_dir(self, 'tweak_book_export_selected', _('Choose location'))
        if path:
            self.export_requested.emit(names, path)

    def replace(self, name):
        c = current_container()
        mt = c.mime_map[name]
        oext = name.rpartition('.')[-1].lower()
        filters = [oext]
        fname = _('Files')
        if mt in OEB_DOCS:
            fname = _('HTML files')
            filters = 'html htm xhtm xhtml shtml'.split()
        elif is_raster_image(mt):
            fname = _('Images')
            filters = 'jpeg jpg gif png'.split()
        path = choose_files(self, 'tweak_book_import_file', _('Choose file'), filters=[(fname, filters)], select_only_single_file=True)
        if not path:
            return
        path = path[0]
        ext = path.rpartition('.')[-1].lower()
        force_mt = None
        if mt in OEB_DOCS:
            force_mt = c.guess_type('a.html')
        nname = os.path.basename(path)
        nname, ext = nname.rpartition('.')[0::2]
        nname = nname + '.' + ext.lower()
        self.replace_requested.emit(name, path, nname, force_mt)

    def link_stylesheets(self, names):
        s = self.categories['styles']
        sheets = [str(s.child(i).data(0, NAME_ROLE) or '') for i in range(s.childCount())]
        if not sheets:
            return error_dialog(self, _('No stylesheets'), _(
                'This book currently has no stylesheets. You must first create a stylesheet'
                ' before linking it.'), show=True)
        d = QDialog(self)
        d.l = l = QVBoxLayout(d)
        d.setLayout(l)
        d.setWindowTitle(_('Choose stylesheets'))
        d.la = la = QLabel(_('Choose the stylesheets to link. Drag and drop to re-arrange'))

        la.setWordWrap(True)
        l.addWidget(la)
        d.s = s = QListWidget(d)
        l.addWidget(s)
        s.setDragEnabled(True)
        s.setDropIndicatorShown(True)
        s.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        s.setAutoScroll(True)
        s.setDefaultDropAction(Qt.DropAction.MoveAction)
        for name in sheets:
            i = QListWidgetItem(name, s)
            flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsSelectable
            i.setFlags(flags)
            i.setCheckState(Qt.CheckState.Checked)
        d.r = r = QCheckBox(_('Remove existing links to stylesheets'))
        r.setChecked(tprefs['remove_existing_links_when_linking_sheets'])
        l.addWidget(r)
        d.bb = bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(d.accept), bb.rejected.connect(d.reject)
        l.addWidget(bb)
        if d.exec() == QDialog.DialogCode.Accepted:
            tprefs['remove_existing_links_when_linking_sheets'] = r.isChecked()
            sheets = [str(s.item(il).text()) for il in range(s.count()) if s.item(il).checkState() == Qt.CheckState.Checked]
            if sheets:
                self.link_stylesheets_requested.emit(names, sheets, r.isChecked())


class NewFileDialog(QDialog):  # {{{

    def __init__(self, parent=None):
        QDialog.__init__(self, parent)
        self.l = l = QVBoxLayout()
        self.setLayout(l)
        self.la = la = QLabel(_(
            'Choose a name for the new (blank) file. To place the file in a'
            ' specific folder in the book, include the folder name, for example: <i>text/chapter1.html'))
        la.setWordWrap(True)
        self.setWindowTitle(_('Choose file'))
        l.addWidget(la)
        self.name = n = QLineEdit(self)
        n.textChanged.connect(self.update_ok)
        l.addWidget(n)
        self.link_css = lc = QCheckBox(_('Automatically add style-sheet links into new HTML files'))
        lc.setChecked(tprefs['auto_link_stylesheets'])
        l.addWidget(lc)
        self.err_label = la = QLabel('')
        la.setWordWrap(True)
        l.addWidget(la)
        self.bb = bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        l.addWidget(bb)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        self.imp_button = b = bb.addButton(_('Import resource file (image/font/etc.)'), QDialogButtonBox.ButtonRole.ActionRole)
        b.setIcon(QIcon.ic('view-image.png'))
        b.setToolTip(_('Import a file from your computer as a new'
                       ' file into the book.'))
        b.clicked.connect(self.import_file)

        self.ok_button = bb.button(QDialogButtonBox.StandardButton.Ok)

        self.file_data = b''
        self.using_template = False
        self.setMinimumWidth(350)

    def show_error(self, msg):
        self.err_label.setText('<p style="color:red">' + msg)
        return False

    def import_file(self):
        path = choose_files(self, 'tweak-book-new-resource-file', _('Choose file'), select_only_single_file=True)
        if path:
            self.do_import_file(path[0])

    def do_import_file(self, path, hide_button=False):
        self.link_css.setVisible(False)
        with open(path, 'rb') as f:
            self.file_data = f.read()
        name = os.path.basename(path)
        fmap = get_recommended_folders(current_container(), (name,))
        if fmap[name]:
            name = '/'.join((fmap[name], name))
        self.name.setText(name)
        self.la.setText(_('Choose a name for the imported file'))
        if hide_button:
            self.imp_button.setVisible(False)

    @property
    def name_is_ok(self):
        return name_is_ok(str(self.name.text()), self.show_error)

    def update_ok(self, *args):
        self.ok_button.setEnabled(self.name_is_ok)

    def accept(self):
        if not self.name_is_ok:
            return error_dialog(self, _('No name specified'), _(
                'You must specify a name for the new file, with an extension, for example, chapter1.html'), show=True)
        tprefs['auto_link_stylesheets'] = self.link_css.isChecked()
        name = str(self.name.text())
        name, ext = name.rpartition('.')[0::2]
        name = (name + '.' + ext.lower()).replace('\\', '/')
        mt = guess_type(name)
        if not self.file_data:
            if mt in OEB_DOCS:
                self.file_data = template_for('html').encode('utf-8')
                if tprefs['auto_link_stylesheets']:
                    data = add_stylesheet_links(current_container(), name, self.file_data)
                    if data is not None:
                        self.file_data = data
                self.using_template = True
            elif mt in OEB_STYLES:
                self.file_data = template_for('css').encode('utf-8')
                self.using_template = True
        self.file_name = name
        QDialog.accept(self)
# }}}


class MergeDialog(QDialog):  # {{{

    def __init__(self, names, parent=None):
        QDialog.__init__(self, parent)
        self.names = names
        self.setWindowTitle(_('Choose master file'))
        self.l = l = QVBoxLayout()
        self.setLayout(l)
        self.la = la = QLabel(_('Choose the master file. All selected files will be merged into the master file:'))
        la.setWordWrap(True)
        l.addWidget(la)
        self.sa = sa = QScrollArea(self)
        l.addWidget(sa)
        self.bb = bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        l.addWidget(bb)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        self.w = w = QWidget(self)
        w.l = QVBoxLayout()
        w.setLayout(w.l)

        buttons = self.buttons = [QRadioButton(n) for n in names]
        buttons[0].setChecked(True)
        for i in buttons:
            w.l.addWidget(i)
        sa.setWidget(w)

        self.resize(self.sizeHint() + QSize(150, 20))

    @property
    def ans(self):
        for n, b in zip(self.names, self.buttons):
            if b.isChecked():
                return n

# }}}


class FileListWidget(QWidget):

    def __init__(self, parent=None):
        QWidget.__init__(self, parent)
        self.setLayout(QGridLayout(self))
        self.file_list = FileList(self)
        self.layout().addWidget(self.file_list)
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.forwarded_signals = {k for k, o in iteritems(vars(self.file_list.__class__)) if isinstance(o, pyqtSignal) and '_' in k and not hasattr(self, k)}
        for x in ('delete_done', 'select_name', 'select_names', 'request_edit', 'mark_name_as_current', 'clear_currently_edited_name'):
            setattr(self, x, getattr(self.file_list, x))
        self.setFocusProxy(self.file_list)
        self.edit_next_file = self.file_list.edit_next_file

    def merge_completed(self, master_name):
        self.file_list.select_name(master_name, set_as_current_index=True)

    def build(self, container, preserve_state=True):
        self.file_list.build(container, preserve_state=preserve_state)

    def restore_temp_names(self):
        self.file_list.restore_temp_names()

    def merge_files(self):
        self.file_list.merge_files()

    @property
    def searchable_names(self):
        return self.file_list.searchable_names

    @property
    def current_name(self):
        return self.file_list.current_name

    def __getattr__(self, name):
        if name in object.__getattribute__(self, 'forwarded_signals'):
            return getattr(self.file_list, name)
        return QWidget.__getattr__(self, name)
