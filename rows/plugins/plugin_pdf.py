from __future__ import unicode_literals

import io
from collections import defaultdict

from pdfminer.converter import PDFPageAggregator, TextConverter
from pdfminer.layout import (LAParams, LTTextBox, LTTextLine, LTChar)
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfinterp import PDFResourceManager, PDFPageInterpreter
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser

from rows.plugins.utils import create_table, get_filename_and_fobj


def text_objects(fobj):
    'For each page inside a PDF, return the list of text objects'

    rsrcmgr = PDFResourceManager()
    laparams = LAParams()
    device = PDFPageAggregator(rsrcmgr, laparams=laparams)
    interpreter = PDFPageInterpreter(rsrcmgr, device)
    parser = PDFParser(fobj)
    doc = PDFDocument(parser)
    parser.set_document(doc)
    assert doc.is_extractable

    for i, page in enumerate(PDFPage.create_pages(doc)):
        interpreter.process_page(page)
        layout = device.get_result()
        objects_in_page = []
        for obj in layout:
            if isinstance(obj, (LTTextBox, LTTextLine, LTChar)):
                objects_in_page.append(obj)
        yield objects_in_page
    fobj.close()


def group_objects(objs, get_attr_function, group_size):
    'Group objects based on an attribute considering a group size'

    objs.sort(key=get_attr_function)
    groups = defaultdict(list)
    for obj in objs:
        attr = get_attr_function(obj)
        found_key = None
        for key in groups.keys():
            if key <= attr <= key + group_size:
                found_key = key
                break
        if not found_key:
            found_key = attr
        groups[found_key].append(obj)

    return groups


def define_intervals(objs, min_attr, max_attr, threshold):
    groups = group_objects(objs, min_attr, threshold)
    intervals = [(key, max_attr(max(value, key=max_attr)))
                 for key, value in groups.items()]
    intervals.sort()
    if not intervals:
        return []

    # Merge overlapping intervals
    result = [intervals[0]]
    for current in intervals[1:]:
        previous = result.pop()
        if current[0] <= previous[1] or current[1] <= previous[1]:
            result.append((previous[0], max((previous[1], current[1]))))
        else:
            result.extend((previous, current))
    return result


def contains_or_overlap(a, b):
    x1min, y1min, x1max, y1max = a
    x2min, y2min, x2max, y2max = b

    contains = x2min >= x1min and x2max <= x1max and \
            y2min >= y1min and y2max <= y1max
    overlaps = (x1min <= x2min <= x1max and y1min <= y2min <= y1max) or \
               (x1min <= x2min <= x1max and y1min <= y2max <= y1max) or \
               (x1min <= x2max <= x1max and y1min <= y2min <= y1max) or \
               (x1min <= x2max <= x1max and y1min <= y2max <= y1max)
    return contains or overlaps


def get_table(objs, x_threshold=0.5, y_threshold=0.5):
    'Where the magic happens'

    # TODO: split this function in many others

    # Define table lines based on objects' y values (with some threshold) -
    # these lines will only be used to determine table boundaries.
    lines = []
    groups = group_objects(objs, lambda obj: obj.y0, y_threshold)
    new_objs = []
    for key, value in groups.items():
        if len(value) < 2:  # Ignore 1-column tables and floating text objects
            continue
        value.sort(key=lambda obj: obj.x0)
        lines.append((key, value))
        new_objs.extend(value)

    # Define table boundaries
    x_min = y_min = float('inf')
    x_max = y_max = float('-inf')
    for _, line_objects in lines:
        for obj in line_objects:
            if obj.x0 <= x_min:
                x_min = obj.x0
            if obj.x1 >= x_max:
                x_max = obj.x1
            if obj.y0 <= y_min:
                y_min = obj.y0
            if obj.y1 >= y_max:
                y_max = obj.y1
    table_bbox = (x_min, y_min, x_max, y_max)

    # Filter out objects outside table boundaries
    objs = [obj for obj in objs
            if contains_or_overlap(table_bbox, obj.bbox)]

    # Define x and y intervals based on filtered objects
    objs.sort(key=lambda obj: obj.x0)
    x_intervals = define_intervals(
            objs,
            min_attr=lambda obj: obj.x0,
            max_attr=lambda obj: obj.x1,
            threshold=x_threshold
    )
    objs.sort(key=lambda obj: -obj.y1)
    y_intervals = define_intervals(
            objs,
            min_attr=lambda obj: obj.y0,
            max_attr=lambda obj: obj.y1,
            threshold=y_threshold
    )

    # Create an empty matrix and fill in with objects
    matrix = [[None] * len(x_intervals) for _ in y_intervals]
    for obj in objs:
        x_index = [index for index, x in enumerate(x_intervals)
                   if x[0] <= obj.x0 and x[1] >= obj.x1][0]
        y_index = [index for index, y in enumerate(y_intervals)
                   if y[0] <= obj.y0 and y[1] >= obj.y1][0]
        matrix[y_index][x_index] = obj.get_text().strip()
    matrix.reverse()

    return matrix


def pdf_table_lines(fobj):
    pages = text_objects(fobj)
    header = None
    for page_index, page in enumerate(pages):
        for line_index, line in enumerate(get_table(page)):
            if line_index == 0:
                if page_index > 0 and line == header:  # skip header repetition
                    continue
                elif page_index == 0:
                    header = line
            yield line


def pdf_to_text(filename_or_fobj):
    'Extract all text objects from a PDF file'

    filename, fobj = get_filename_and_fobj(filename_or_fobj, mode='rb')

    # TODO: specify page range

    parser = PDFParser(fobj)
    doc = PDFDocument(parser)
    parser.set_document(doc)
    assert doc.is_extractable

    for page in PDFPage.create_pages(doc):
        rsrcmgr = PDFResourceManager()
        laparams = LAParams()
        result = io.StringIO()
        device = TextConverter(rsrcmgr, result, laparams=laparams)
        interpreter = PDFPageInterpreter(rsrcmgr, device)
        interpreter.process_page(page)
        yield result.getvalue()


def import_from_pdf(filename_or_fobj, *args, **kwargs):
    filename, fobj = get_filename_and_fobj(filename_or_fobj, mode='rb')

    # TODO: pass threshold to pdf_table_lines (and from there to get_pages)
    # TODO: specify page range
    meta = {
            'imported_from': 'pdf',
            'filename': filename,
    }
    return create_table(pdf_table_lines(fobj), meta=meta, *args, **kwargs)