"""caj2pdf: drive libreaderex_x64.so's CAJFILE_* C API to convert .caj -> .pdf.

Submodules
----------
cajlib   ctypes binding for the shipped CAJ viewer library (the source of truth).
_pdf     hand-rolled PDF 1.7 writer: pages, images, Type0/CIDFontType2 embedded fonts.
_img     page-bitmap -> PDF image XObject encoder (flate/PNG-predictor/DCT/bilevel).
ttfsub   hand-rolled TrueType subsetter (no fontTools in this environment).
"""
from . import cajlib  # noqa: F401

__all__ = ["cajlib"]
__version__ = "1.0"
