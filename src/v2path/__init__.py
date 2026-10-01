"""V2Path: source-preserving HL7 v2 manipulation. GPL-3.0-or-later."""
from .core import NULL, EncodingCharacters, Message, Segment, ValueState, parse
from .errors import AssignmentError, Diagnostic, ParseError, StaleViewError

__version__ = '0.3.0'
__all__ = ['NULL', 'EncodingCharacters', 'Message', 'Segment', 'ValueState', 'parse',
           'AssignmentError', 'Diagnostic', 'ParseError', 'StaleViewError']
from .structure import BoundaryContext, Grammar, Group, GroupRule, ResolvedMessage, SegmentView
from . import presets
__all__ += ['BoundaryContext', 'Grammar', 'Group', 'GroupRule', 'ResolvedMessage', 'SegmentView', 'presets']
from .io import Envelope, FrameEnd, FrameStart, MessageWriter, iter_messages, iter_records, transform_stream
__all__ += ['Envelope', 'FrameEnd', 'FrameStart', 'MessageWriter', 'iter_messages', 'iter_records', 'transform_stream']
