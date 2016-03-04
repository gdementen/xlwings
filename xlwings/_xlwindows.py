import os
import sys

# Hack to find pythoncom.dll - needed for some distribution/setups (includes seemingly unused import win32api)
# E.g. if python is started with the full path outside of the python path, then it almost certainly fails
cwd = os.getcwd()
if not hasattr(sys, 'frozen'):
    # cx_Freeze etc. will fail here otherwise
    os.chdir(sys.exec_prefix)
import win32api

os.chdir(cwd)

from warnings import warn
import pywintypes
import pythoncom
from win32com.client import dynamic, Dispatch, CDispatch, DispatchEx
import win32timezone
import win32gui
import datetime as dt
from .constants import Direction, ColorIndex
from .utils import rgb_to_int, int_to_rgb, get_duplicates, np_datetime_to_datetime
from ctypes import oledll, PyDLL, py_object, byref, POINTER
from comtypes import IUnknown
from comtypes.automation import IDispatch

# Optional imports
try:
    import pandas as pd
except ImportError:
    pd = None
try:
    import numpy as np
except ImportError:
    np = None

from . import PY3

# Time types: pywintypes.timetype doesn't work on Python 3
time_types = (dt.date, dt.datetime, type(pywintypes.Time(0)))
if np:
    time_types = time_types + (np.datetime64,)


# Constants
OBJID_NATIVEOM = -16


def accessible_object_from_window(hwnd):
    ptr = POINTER(IDispatch)()
    res = oledll.oleacc.AccessibleObjectFromWindow(
        hwnd, OBJID_NATIVEOM,
        byref(IDispatch._iid_), byref(ptr))
    return ptr


def comtypes_to_pywin(ptr, interface=None):
    _PyCom_PyObjectFromIUnknown = PyDLL(pythoncom.__file__).PyCom_PyObjectFromIUnknown
    _PyCom_PyObjectFromIUnknown.restype = py_object

    if interface is None:
        interface = IUnknown
    return _PyCom_PyObjectFromIUnknown(ptr, byref(interface._iid_), True)


def get_xl_app_from_hwnd(hwnd):
    child_hwnd = win32gui.FindWindowEx(hwnd, 0, 'XLDESK', None)
    child_hwnd = win32gui.FindWindowEx(child_hwnd, 0, 'EXCEL7', None)

    ptr = accessible_object_from_window(child_hwnd)
    p = comtypes_to_pywin(ptr, interface=IDispatch)
    disp = Dispatch(p)
    return disp.Application


def get_excel_hwnds():
    hwnds = []
    win32gui.EnumWindows(lambda hwnd, result_list: result_list.append(hwnd), hwnds)

    excel_hwnds = []
    for hwnd in hwnds:
        try:
            # Apparently, this fails on some systems when Excel is closed
            if win32gui.FindWindowEx(hwnd, 0, 'XLDESK', None):
                excel_hwnds.append(hwnd)
        except pywintypes.error:
            pass
    return excel_hwnds


def get_xl_apps():
    xl_apps = []
    hwnds = get_excel_hwnds()
    for hwnd in hwnds:
        try:
            xl_app = get_xl_app_from_hwnd(hwnd)
            xl_apps.append(xl_app)
        except WindowsError:
            # This happens if the bare Excel Application is open without Workbook
            # i.e. there is no 'EXCEL7' child hwnd that would be necessary to make a connection
            pass
    return xl_apps


def get_all_open_xl_workbooks(xl_app):
    return [xl_workbook for xl_workbook in xl_app.Workbooks]


def is_file_open(fullname):
    if not PY3:
        if isinstance(fullname, str):
            fullname = unicode(fullname, 'mbcs')
    open_workbooks = []
    for xl_app in get_xl_apps():
        open_fullnames = [i.FullName.lower() for i in get_all_open_xl_workbooks(xl_app)]
        for fn in open_fullnames:
            open_workbooks.append(fn)
    return fullname.lower() in open_workbooks


def get_duplicate_fullnames():
    """Returns a list of fullnames that are opened in multiple instances"""
    open_xl_workbooks = []
    for xl_app in get_xl_apps():
        for xl_workbook in get_all_open_xl_workbooks(xl_app):
            open_xl_workbooks.append(xl_workbook)
    return get_duplicates([i.FullName.lower() for i in open_xl_workbooks])


def get_open_workbook(fullname, app_target=None, hwnd=None):
    """
    Returns the COM Application and Workbook objects of an open Workbook.
    While GetObject() would return the correct Excel instance if there are > 1,
    it cannot cope with Workbooks that don't appear in the ROT (happens with
    untrusted locations).
    """
    if app_target is not None:
        raise NotImplementedError('app_target is only available on Mac.')
    if not PY3:
        if isinstance(fullname, str):
            fullname = unicode(fullname, 'mbcs')
    duplicate_fullnames = get_duplicate_fullnames()

    if hwnd is None:
        xl_apps = get_xl_apps()
    else:
        hwnd = int(hwnd)  # should it need to be long in PY2?
        xl_apps = [get_xl_app_from_hwnd(hwnd)]

    for xl_app in xl_apps:
        for xl_workbook in get_all_open_xl_workbooks(xl_app):
            if (
                xl_workbook.FullName.lower() == fullname.lower() or
                xl_workbook.Name.lower() == fullname.lower()
               ):
                if (xl_workbook.FullName.lower() not in duplicate_fullnames) or (hwnd is not None):
                    return xl_app, xl_workbook
                else:
                    warn('This Workbook is open in multiple instances.'
                         'The connection was made with the one that was last active.')
                    return xl_app, xl_workbook


def is_range_instance(xl_range):
    pyid = getattr(xl_range, '_oleobj_', None)
    if pyid is None:
        return False
    return xl_range._oleobj_.GetTypeInfo().GetTypeAttr().iid == pywintypes.IID('{00020846-0000-0000-C000-000000000046}')
    # return pyid.GetTypeInfo().GetDocumentation(-1)[0] == 'Range'


class Application(object):

    def __init__(self, xl=None):
        if xl is None:
            # new instance
            self.xl = Application(DispatchEx('Excel.Application'))
        else:
            self.xl = xl

    @classmethod
    def get_running(cls):
        return Application(dynamic.Dispatch('Excel.Application'))

    def get_active_workbook(self):
        return Workbook(self.xl.ActiveWorkbook)

    def get_active_sheet(self):
        return Sheet(self.xl.ActiveSheet)

    def open_workbook(self, fullname):
        return Workbook(self.xl.Workbooks.Open(fullname))

    def new_workbook(self):
        return Workbook(self.xl.Workbooks.Add())

    @property
    def selection(self):
        return Range(self.Selection)

    @property
    def visible(self):
        return self.xl.Visible

    @visible.setter
    def visible(self, visible):
        self.xl.Visible = visible

    def quit(self):
        self.xl.DisplayAlerts = False
        self.xl.Quit()
        self.xl.DisplayAlerts = True

    @property
    def screen_updating(self):
        return self.xl.ScreenUpdating

    @screen_updating.setter
    def screen_updating(self, value):
        self.xl.ScreenUpdating = value

    @property
    def calculation(self):
        return self.xl.Calculation

    @calculation.setter
    def calculation(self, value):
        self.xl.Calculation = value

    def calculate(self):
        self.xl.Calculate()

    def get_version_string(self):
        return self.lx.Version

    def get_major_version_number(self):
        return int(self.get_version_string().split('.')[0])


class Workbook(object):

    def __init__(self, xl):
        self.xl = xl

    def get_name(self):
        return self.xl.Name

    def set_name(self, value):
        self.xl.Name = value

    def get_index(self):
        return self.xl.Index

    def get_sheet(self, sheet_name_or_index):
        return Sheet(self.xl.Sheets(sheet_name_or_index))

    @property
    def application(self):
        return Application(self.xl.Application)

    def close(self):
        self.xl.Close(SaveChanges=False)

    @property
    def active_sheet(self):
        return Sheet(self.xl.ActiveSheet)
    
    def add_sheet(self, before, after):
        if before:
            return Sheet(self.xl.Worksheets.Add(Before=before.xl_sheet))
        else:
            # Hack, since "After" is broken in certain environments
            # see: http://code.activestate.com/lists/python-win32/11554/
            count = self.xl.Worksheets.Count
            new_sheet_index = after.xl_sheet.Index + 1
            if new_sheet_index > count:
                xl_sheet = self.xl.Worksheets.Add(Before=self.xl.Sheets(after.xl_sheet.Index))
                self.xl.Worksheets(self.xl.Worksheets.Count)\
                    .Move(Before=self.xl.Sheets(self.xl.Worksheets.Count - 1))
                self.xl.Worksheets(self.xl.Worksheets.Count).Activate()
            else:
                xl_sheet = self.xl.Worksheets.Add(Before=self.xl.Sheets(after.xl_sheet.Index + 1))
            return Sheet(xl_sheet)
    
    def count_sheets(self):
        return self.xl.Worksheets.Count

    def save_workbook(self, path):
        saved_path = self.xl.Path
        if (saved_path != '') and (path is None):
            # Previously saved: Save under existing name
            self.xl.Save()
        elif (saved_path == '') and (path is None):
            # Previously unsaved: Save under current name in current working directory
            path = os.path.join(os.getcwd(), self.xl.Name + '.xlsx')
            self.xl.Application.DisplayAlerts = False
            self.xl.SaveAs(path)
            self.xl.Application.DisplayAlerts = True
        elif path:
            # Save under new name/location
            self.xl.Application.DisplayAlerts = False
            self.xl.SaveAs(path)
            self.xl.Application.DisplayAlerts = True
    
    @property
    def fullname(self):
        return self.xl.FullName
    
    def set_names(self, names):
        for i in self.xl.Names:
            names[i.Name] = i
    
    def delete_name(self, name):
        self.xl.Names(name).Delete()


class Sheet(object):

    def __init__(self, xl):
        self.xl = xl

    def get_name(self):
        return self.xl.Name

    def get_workbook(self):
        return Workbook(self.Parent)

    def get_range(self, address):
        return Range(self.xl.Range(address))

    def activate(self):
        return self.xl.Activate()

    def get_value_from_index(self, row_index, column_index):
        return self.xl.Cells(row_index, column_index).Value

    def clear_contents(self):
        self.xl.Cells.ClearContents()

    def clear(self):
        self.xl.Cells.Clear()

    def get_row_index_end_down(self, row_index, column_index):
        return Range(self.xl.Cells(row_index, column_index).End(Direction.xlDown).Row)

    def get_column_index_end_right(self, row_index, column_index):
        return Range(self.xl.Cells(row_index, column_index).End(Direction.xlToRight).Column)

    def get_current_region_address(self, row_index, column_index):
        return str(self.xl.Cells(row_index, column_index).CurrentRegion.Address)

    def autofit(self, axis):
        if axis == 'rows' or axis == 'r':
            self.xl.Rows.AutoFit()
        elif axis == 'columns' or axis == 'c':
            self.xl.Columns.AutoFit()
        elif axis is None:
            self.xl.Rows.AutoFit()
            self.xl.Columns.AutoFit()

    def get_range_from_indices(self, first_row, first_column, last_row, last_column):
        return Range(self.xl.Range(self.xl.Cells(first_row, first_column), self.xl.Cells(last_row, last_column)))

    def delete(self):
        xl_app = self.xl.Parent.Application
        xl_app.DisplayAlerts = False
        self.xl.Delete()
        xl_app.DisplayAlerts = True


class Range(object):

    def __init__(self, xl):
        self.xl = xl

    def get_worksheet(self):
        return Sheet(self.xl.Worksheet)

    def get_coordinates(self):
        row1 = self.xl.Row
        col1 = self.xl.Column
        row2 = row1 + self.xl.Rows.Count - 1
        col2 = col1 + self.xl.Columns.Count - 1
        return (row1, col1, row2, col2)

    def get_first_row(self):
        return self.xl.Row

    def get_first_column(self):
        return self.xl.Column

    def count_rows(self):
        return self.xl.Rows.Count

    def count_columns(self):
        return self.xl.Columns.Count

    def get_value_from_range(self):
        return self.xl.Value

    def set_value(self, data):
        self.xl.Value = data

    def clear_contents(self):
        self.xl.ClearContents()

    def clear(self):
        self.xl.Clear()

    def get_formula(self):
        return self.xl.Formula

    def set_formula(self, value):
        self.xl.Formula = value

    def get_column_width(self):
        return self.xl.ColumnWidth

    def set_column_width(self, value):
        self.xl.ColumnWidth = value

    def get_row_height(self):
        return self.xl.RowHeight

    def set_row_height(self, value):
        self.xl.RowHeight = value

    def get_width(self):
        return self.xl.Width

    def get_height(self):
        return self.xl.Height

    def get_left(self):
        return self.xl.Left

    def get_top(self):
        return self.xl.Top

    def get_number_format(self):
        return self.xl.NumberFormat

    def set_number_format(self, value):
        self.xl.NumberFormat = value

    def get_address(self, row_absolute, col_absolute, external):
        return self.xl.GetAddress(row_absolute, col_absolute, 1, external)

    def autofit(self, axis):
        if axis == 'rows' or axis == 'r':
            self.lx.Rows.AutoFit()
        elif axis == 'columns' or axis == 'c':
            self.lx.Columns.AutoFit()
        elif axis is None:
            self.lx.Columns.AutoFit()
            self.lx.Rows.AutoFit()

    def get_hyperlink_address(self):
        try:
            return self.xl.Hyperlinks(1).Address
        except pywintypes.com_error:
            raise Exception("The cell doesn't seem to contain a hyperlink!")

    def set_hyperlink(self, address, text_to_display=None, screen_tip=None):
        # Another one of these pywin32 bugs that only materialize under certain circumstances:
        # http://stackoverflow.com/questions/6284227/hyperlink-will-not-show-display-proper-text
        link = self.xl.Hyperlinks.Add(Anchor=self.xl, Address=address)
        link.TextToDisplay = text_to_display
        link.ScreenTip = screen_tip

    def set_color(self, color_or_rgb):
        if color_or_rgb is None:
            self.xl.Interior.ColorIndex = ColorIndex.xlColorIndexNone
        elif isinstance(color_or_rgb, int):
            self.xl.Interior.Color = color_or_rgb
        else:
            self.xl.Interior.Color = rgb_to_int(color_or_rgb)

    def get_color(self):
        if self.xl.Interior.ColorIndex == ColorIndex.xlColorIndexNone:
            return None
        else:
            return int_to_rgb(self.xl.Interior.Color)

    def set_named_range(self, value):
        self.xl.Name = value

    def get_named_range(self):
        try:
            name = self.xl.Name.Name
        except pywintypes.com_error:
            name = None
        return name


def clean_value_data(data, datetime_builder, empty_as, number_builder):
    if number_builder is not None:
        return [
            [
                _com_time_to_datetime(c, datetime_builder)
                if isinstance(c, time_types) else
                number_builder(c)
                if type(c) == float else
                empty_as
                if c is None else
                c
                for c in row
            ]
            for row in data
        ]
    else:
        return [
            [
                _com_time_to_datetime(c, datetime_builder)
                if isinstance(c, time_types)
                else empty_as
                if c is None
                else c
                for c in row
            ]
            for row in data
        ]


def _com_time_to_datetime(com_time, datetime_builder):
    """
    This function is a modified version from Pyvot (https://pypi.python.org/pypi/Pyvot)
    and subject to the following copyright:

    Copyright (c) Microsoft Corporation.

    This source code is subject to terms and conditions of the Apache License, Version 2.0. A
    copy of the license can be found in the LICENSE.txt file at the root of this distribution. If
    you cannot locate the Apache License, Version 2.0, please send an email to
    vspython@microsoft.com. By using this source code in any fashion, you are agreeing to be bound
    by the terms of the Apache License, Version 2.0.

    You must not remove this notice, or any other, from this software.

    """

    if PY3:
        # The py3 version of pywintypes has its time type inherit from datetime.
        # We copy to a new datetime so that the returned type is the same between 2/3
        # Changed: We make the datetime object timezone naive as Excel doesn't provide info
        return datetime_builder(month=com_time.month, day=com_time.day, year=com_time.year,
                           hour=com_time.hour, minute=com_time.minute, second=com_time.second,
                           microsecond=com_time.microsecond, tzinfo=None)
    else:
        assert com_time.msec == 0, "fractional seconds not yet handled"
        return datetime_builder(month=com_time.month, day=com_time.day, year=com_time.year,
                           hour=com_time.hour, minute=com_time.minute, second=com_time.second)


def _datetime_to_com_time(dt_time):
    """
    This function is a modified version from Pyvot (https://pypi.python.org/pypi/Pyvot)
    and subject to the following copyright:

    Copyright (c) Microsoft Corporation.

    This source code is subject to terms and conditions of the Apache License, Version 2.0. A
    copy of the license can be found in the LICENSE.txt file at the root of this distribution. If
    you cannot locate the Apache License, Version 2.0, please send an email to
    vspython@microsoft.com. By using this source code in any fashion, you are agreeing to be bound
    by the terms of the Apache License, Version 2.0.

    You must not remove this notice, or any other, from this software.

    """
    # Convert date to datetime
    if np:
        if type(dt_time) is np.datetime64:
            dt_time = np_datetime_to_datetime(dt_time)

    if type(dt_time) is dt.date:
        dt_time = dt.datetime(dt_time.year, dt_time.month, dt_time.day,
                              tzinfo=win32timezone.TimeZoneInfo.utc())

    if PY3:
        # The py3 version of pywintypes has its time type inherit from datetime.
        # For some reason, though it accepts plain datetimes, they must have a timezone set.
        # See http://docs.activestate.com/activepython/2.7/pywin32/html/win32/help/py3k.html
        # We replace no timezone -> UTC to allow round-trips in the naive case
        if pd and isinstance(dt_time, pd.tslib.Timestamp):
            # Otherwise pandas prints ignored exceptions on Python 3
            dt_time = dt_time.to_datetime()
        # We don't use pytz.utc to get rid of additional dependency
        # Don't do any timezone transformation: simply cutoff the tz info
        # If we don't reset it first, it gets transformed into UTC before transferred to Excel
        dt_time = dt_time.replace(tzinfo=None)
        dt_time = dt_time.replace(tzinfo=win32timezone.TimeZoneInfo.utc())

        return dt_time
    else:
        assert dt_time.microsecond == 0, "fractional seconds not yet handled"
        return pywintypes.Time(dt_time.timetuple())


def prepare_xl_data_element(x):
    if isinstance(x, time_types):
        return _datetime_to_com_time(x)
    else:
        return x


def get_chart_object(xl_workbook, sheet_name_or_index, chart_name_or_index):
    return xl_workbook.Sheets(sheet_name_or_index).ChartObjects(chart_name_or_index)


def get_chart_index(xl_chart):
    return xl_chart.Index


def get_chart_name(xl_chart):
    return xl_chart.Name


def add_chart(xl_workbook, sheet_name_or_index, left, top, width, height):
    return xl_workbook.Sheets(sheet_name_or_index).ChartObjects().Add(left, top, width, height)


def set_chart_name(xl_chart, name):
    xl_chart.Name = name


def set_source_data_chart(xl_chart, xl_range):
    xl_chart.Chart.SetSourceData(xl_range)


def get_chart_type(xl_chart):
    return xl_chart.Chart.ChartType


def set_chart_type(xl_chart, chart_type):
    xl_chart.Chart.ChartType = chart_type


def activate_chart(xl_chart):
    xl_chart.Activate()


def open_template(fullpath):
    os.startfile(fullpath)


def get_picture(picture):
    return picture.xl_workbook.Sheets(picture.sheet_name_or_index).Pictures(picture.name_or_index)


def get_picture_index(picture):
    return picture.xl_picture.Index


def get_picture_name(xl_picture):
    return xl_picture.Name


def get_shape(shape):
    return shape.xl_workbook.Sheets(shape.sheet_name_or_index).Shapes(shape.name_or_index)


def get_shape_name(shape):
    return shape.xl_shape.Name


def set_shape_name(xl_workbook, sheet_name_or_index, xl_shape, value):
    xl_workbook.Sheets(sheet_name_or_index).Shapes(xl_shape.Name).Name = value
    return xl_workbook.Sheets(sheet_name_or_index).Shapes(value)


def get_shapes_names(xl_workbook, sheet):
    shapes = xl_workbook.Sheets(sheet).Shapes
    if shapes is not None:
        return [i.Name for i in shapes]
    else:
        return []


def get_shape_left(shape):
    return shape.xl_shape.Left


def set_shape_left(shape, value):
    shape.xl_shape.Left = value


def get_shape_top(shape):
    return shape.xl_shape.Top


def set_shape_top(shape, value):
    shape.xl_shape.Top = value


def get_shape_width(shape):
    return shape.xl_shape.Width


def set_shape_width(shape, value):
    shape.xl_shape.Width = value


def get_shape_height(shape):
    return shape.xl_shape.Height


def set_shape_height(shape, value):
    shape.xl_shape.Height = value


def delete_shape(shape):
    shape.xl_shape.Delete()


def add_picture(xl_workbook, sheet_name_or_index, filename, link_to_file, save_with_document, left, top, width, height):
    return xl_workbook.Sheets(sheet_name_or_index).Shapes.AddPicture(Filename=filename,
                                                                     LinkToFile=link_to_file,
                                                                     SaveWithDocument=save_with_document,
                                                                     Left=left,
                                                                     Top=top,
                                                                     Width=width,
                                                                     Height=height)


