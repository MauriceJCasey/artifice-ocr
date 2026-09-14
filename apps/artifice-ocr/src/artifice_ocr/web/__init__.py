# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Web frontend for the OCR pipeline (spike).

Additive to the tkinter GUI, not a replacement for it: `server.py` is the only
new entry point, and everything it depends on (`jobs`, `pipeline`, `history`,
`tropy`, `config`) is unchanged core code the tkinter build already uses.
"""
