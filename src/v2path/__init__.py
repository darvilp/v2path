"""V2Path: source-preserving HL7 v2 manipulation. GPL-3.0-or-later."""
from .core import NULL, EncodingCharacters, Message, Segment, ValueState, parse
from .errors import AssignmentError, Diagnostic, ParseError, StaleViewError

__version__ = '0.1.0'
__all__ = ['NULL', 'EncodingCharacters', 'Message', 'Segment', 'ValueState', 'parse',
           'AssignmentError', 'Diagnostic', 'ParseError', 'StaleViewError']
