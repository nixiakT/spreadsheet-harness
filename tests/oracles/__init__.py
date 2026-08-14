"""Independent test oracles for spreadsheet artifact assertions."""

from .ooxml_semantic_oracle import OracleCell, OracleDiff, diff_ooxml

__all__ = ["OracleCell", "OracleDiff", "diff_ooxml"]
