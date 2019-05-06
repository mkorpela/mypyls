# Copyright 2017 Palantir Technologies, Inc.
import logging
from pyls import hookimpl, uris
from mypy.suggestions import SuggestionEngine, get_definition
from mypy.util import short_type

from mypy.nodes import (
    FuncDef, MypyFile, SymbolTable,
    SymbolNode, TypeInfo, Node, Expression, ReturnStmt, NameExpr, SymbolTableNode, Var,
    AssignmentStmt, Context, RefExpr, FuncBase, MemberExpr, ImportBase, Import, ImportAll, ImportFrom
)
from mypy.types import (
    Type, AnyType, TypeOfAny, CallableType, UnionType, NoneTyp, Instance, is_optional,
)

from typing import Optional, Tuple, List
import parser
import symbol
import token

log = logging.getLogger(__name__)


@hookimpl
def pyls_definitions(config, workspace, document, position):
    engine = SuggestionEngine(workspace.mypy_server.fine_grained_manager)
    definition = find_definition(engine, document.path, position['line'], position['character'])
    if definition is None:
        return []
    path, line, column = definition
    return [{
        'uri': uris.from_fs_path(path),
        'range': {
            'start': {'line': line - 1, 'character': column},
            'end': {'line': line - 1, 'character': column}
        }
    }]

def find_definition(engine, path, line, column) -> Optional[Tuple[str, int, int]]:
    # Columns are zero based in the AST, but rows are 1-based.
    line = line + 1
    node, mypy_file = engine.find_name_expr(path, line, column)

    if node is None:
        log.info('No name expression at this location')
        return None

    def_node = None
    result = ''
    if isinstance(node, NameExpr):
        log.info("Find definition of '%s' (%s:%s)" % (node.name, node.line, node.column + 1))
        def_node = node.node
    elif isinstance(node, Instance):
        log.info("Find definition of '%s' at (%s:%s)" % (node.type.fullname(), node.line, node.column + 1))
        def_node = node.type.defn
    elif isinstance(node, MemberExpr):
        log.info("Find definition of '%s' (%s:%s)" % (node.name, node.line, node.column + 1))
        def_node = get_definition(node, engine.manager.all_types)
    elif isinstance(node, ImportBase):
        log.info("Find definition of import (%s:%s)" % (node.line, node.column + 1))
        def_node = get_import_definition(engine.manager, node, mypy_file, line, column, path)
    else:
        logging.error(f'Unknown expression: {short_type(node)}')
        
    if def_node is None:
        logging.error('Definition not found')
        return None
    
    filename = engine.get_file(def_node, mypy_file)
    if filename is None:
        log.info("Could not find file name, guessing symbol is defined in same file.")
        filename = path
    # Column is zero-based. Sometimes returns -1 :\
    column = 0 if def_node.column == -1 else def_node.column
    log.info("Definition at %s:%s:%s (%s)" % (filename, def_node.line, column, short_type(def_node)))
    
    return filename, def_node.line, column

def get_import_definition(manager, import_node: Node, mypy_file: MypyFile, line: int, column: int, path: str) -> Optional[Node]:
    # lines are 1 based, cols 0 based.

    with open(path) as file:
        code_lines: List[str] = file.readlines()

    if import_node.line == import_node.end_line:
        import_code = code_lines[import_node.line-1][import_node.column:import_node.end_column]
    else:
        first_line = code_lines[import_node.line-1][import_node.column:]
        intermediate_lines = ''.join(code_lines[import_node.line:import_node.end_line-1])
        last_line = code_lines[import_node.end_line-1][:import_node.end_column]
        import_code = first_line + intermediate_lines + last_line
    
    suite = parser.suite(import_code).tolist(True, True)
    line_relative_to_import = line - import_node.line + 1
    column_relative_to_import = column
    if line == import_node.line:
        column_relative_to_import -= import_node.column
    module_name, name = find_import_name(import_node, line_relative_to_import, column_relative_to_import, suite)
    if not module_name:
        return None
    return manager.modules[module_name]

def find_import_name(import_node, line, column, suite):
    assert suite[0] == symbol.file_input
    stmt = suite[1]
    assert stmt[0] == symbol.stmt
    simple_stmt = stmt[1]
    assert simple_stmt[0] == symbol.simple_stmt
    small_stmt = simple_stmt[1]
    assert small_stmt[0] == symbol.small_stmt
    import_stmt = small_stmt[1]
    assert import_stmt[0] == symbol.import_stmt

    if isinstance(import_node, Import):
        import_name = import_stmt[1]
        assert import_name[0] == symbol.import_name
        dotted_as_names = import_name[2]
        for dotted_as_name in dotted_as_names[1:]:
            if dotted_as_name[0] != symbol.dotted_as_name:
                continue
            dotted_name = dotted_as_name[1]
            name = get_dotted_name_at_position(dotted_name, line, column)
            if name:
                return name, None
    elif isinstance(import_node, ImportAll):
        import_from = import_stmt[1]
        assert import_from[0] == symbol.import_from
        leading_dots = ''
        contained_in_leading_dots = False
        for element in import_from[2:]:
            if element[0] == token.NAME and element[1] == 'import':
                break
            if element[0] in (token.DOT, token.ELLIPSIS):
                leading_dots += element[1]
                if not contained_in_leading_dots:
                    contained_in_leading_dots = token_contains_offset(element[2], element[3], len(element[1]), line, column)
            elif element[0] == symbol.dotted_name:
                if contained_in_leading_dots:
                    name_token = element[1]
                    assert name_token[0] == token.NAME
                    first_name = name_token[1]
                    return leading_dots + first_name, None
                else:
                    dotted_name = get_dotted_name_at_position(element, line, column)
                    if dotted_name:
                        return leading_dots + dotted_name, None
    elif isinstance(import_node, ImportFrom):
        pass

    return None, None


def get_dotted_name_at_position(dotted_name, line, column):
    assert dotted_name[0] == symbol.dotted_name
    modules = []
    for dotted_name_part in dotted_name[1:]:
        token_type, name, name_line, name_column = dotted_name_part
        if token_type != token.NAME:
            continue
        modules.append(name)
        if token_contains_offset(name_line, name_column, len(name), line, column):
            return '.'.join(modules)

def token_contains_offset(token_line, token_column, token_length, line, column):
    if token_line != line:
        return False
    
    return token_column <= column <= token_column + token_length
