# Copyright 2017 Palantir Technologies, Inc.
import contextlib
import logging
import os
import re
import sys
from collections import defaultdict
import pyls.uris

from mypy.dmypy_server import Server
from mypy.dmypy_util import DEFAULT_STATUS_FILE
from mypy.options import Options

from pyls import hookimpl, lsp
from threading import Thread

line_pattern = r"([^:]+):(?:(\d+):)?(?:(\d+):)? (\w+): (.*)"

log = logging.getLogger(__name__)

@hookimpl
def pyls_initialize(config, workspace):
    thread = Thread(target=mypy_check, args=(workspace, ))
    thread.start()

def mypy_check(workspace):
    options = Options()
    options.show_column_numbers = True
    workspace.dmypy = Server(options, DEFAULT_STATUS_FILE)
    log.info('Checking mypy...')
    try:
        result = workspace.dmypy.cmd_check([workspace.root_path])
        log.info(f'mypy done, exit code {result["status"]}')
        if result['err']:
            log.info(f'mypy stderr:\n{result["err"]}')
        if result['out']:
            log.info(f'mypy stdout:\n{result["out"]}')
            publish_diagnostics(workspace, result['out'])
    except Exception:
        log.exception('Error in mypy check:')
    except SystemExit:
        log.exception('Oopsy, mypy tried to exit.')


def parse_line(line):
    result = re.match(line_pattern, line)
    if result is None:
        log.info(f'Skipped unrecognized mypy line: {line}')
        return None, None

    path, lineno, offset, severity, msg = result.groups()
    lineno = int(lineno or 1)
    offset = int(offset or 0)
    errno = 2
    if severity == 'error':
        errno = 1
    diag = {
        'source': 'mypy',
        'range': {
            'start': {'line': lineno - 1, 'character': offset},
            # There may be a better solution, but mypy does not provide end
            'end': {'line': lineno - 1, 'character': offset}
        },
        'message': msg,
        'severity': errno
    }

    return path, diag


def parse_mypy_output(mypy_output):
    diagnostics = defaultdict(list)
    for line in mypy_output.splitlines():
        path, diag = parse_line(line)
        if diag:
            diagnostics[path].append(diag)

    return diagnostics


def publish_diagnostics(workspace, mypy_output):
    diagnostics_by_path = parse_mypy_output(mypy_output)
    for path, diagnostics in diagnostics_by_path.items():
        uri = pyls.uris.from_fs_path(os.path.join(workspace.root_path, path))
        workspace.publish_diagnostics(uri, diagnostics)
