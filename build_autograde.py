#!/usr/bin/env python3

import os
import re
import sys
import enum
import shutil
import zipfile
import argparse
import dataclasses
import collections
from typing import List, Iterable, Tuple, Callable

import json
import hashlib
import logging
import itertools

import ipynb_metadata
import ipynb_util
import judge_setting

if (sys.version_info.major, sys.version_info.minor) < (3, 7):
    print('[ERROR] This script requires Python >= 3.7.')
    sys.exit(1)

INTRODUCTION_FILE = 'intro.ipynb'

CONF_DIR = 'autograde'

SUBMISSION_CELL_FORMAT = """
##########################################################
##  <[ {exercise_key} ]> 解答セル (Answer cell)
##  このコメントの書き変えを禁ず (Never edit this comment)
##########################################################

{content}""".strip()

REDIRECTION_CELL_FORMAT = """
# このセルではなく {redirect_to} を使ってください
# Use {redirect_to} instead of this cell
""".strip()

class FieldKey(enum.Enum):
    WARNING = enum.auto()
    CONTENT = enum.auto()
    STUDENT_CODE_CELL = enum.auto()
    EXPLANATION = enum.auto()
    ANSWER_EXAMPLES = enum.auto()
    STUDENT_TESTS = enum.auto()
    SYSTEM_TEST_CASES = enum.auto()
    SYSTEM_TEST_CASES_EXECUTE_CELL = enum.auto()
    SYSTEM_TEST_SETTING = enum.auto()

class FieldProperty(enum.Flag):
    SINGLE = enum.auto()
    LIST = enum.auto()
    OPTIONAL = enum.auto()
    MARKDOWN_HEADED = enum.auto()
    FILE = enum.auto()
    CODE = enum.auto()

FieldKey.WARNING.properties = FieldProperty(0)
FieldKey.CONTENT.properties = FieldProperty.LIST | FieldProperty.MARKDOWN_HEADED
FieldKey.STUDENT_CODE_CELL.properties = FieldProperty.SINGLE | FieldProperty.CODE
FieldKey.EXPLANATION.properties = FieldProperty.LIST | FieldProperty.MARKDOWN_HEADED | FieldProperty.OPTIONAL
FieldKey.ANSWER_EXAMPLES.properties = FieldProperty.LIST | FieldProperty.OPTIONAL
FieldKey.STUDENT_TESTS.properties = FieldProperty.LIST | FieldProperty.OPTIONAL
FieldKey.SYSTEM_TEST_CASES.properties = FieldProperty.LIST | FieldProperty.FILE
FieldKey.SYSTEM_TEST_CASES_EXECUTE_CELL.properties = FieldProperty.SINGLE | FieldProperty.CODE
FieldKey.SYSTEM_TEST_SETTING.properties = FieldProperty.SINGLE

CellType = ipynb_util.NotebookCellType

class Cell(collections.namedtuple('Cell', ('cell_type', 'source'))):
    def to_ipynb(self):
        if self.cell_type == CellType.CODE:
            return {'cell_type': self.cell_type.value,
                    'execution_count': None,
                    'metadata': {},
                    'outputs': [],
                    'source': self.source.splitlines(True)}
        else:
            return {'cell_type': self.cell_type.value,
                    'metadata': {},
                    'source': self.source.splitlines(True)}

@dataclasses.dataclass
class Exercise:
    key: str        # Key string
    dirpath: str    # Directory path
    version: str    # Version string
    title: str      # Title string
    content: List[Cell]                  # The description of exercise, starts with multiple '#'s
    student_code_cell: Cell              # Code cell
    explanation: List[Cell]              # The explanation of exercise, starts with multiple '#'s
    answer_examples: List[Cell]          # List of (filename, content, original code cell)
    student_tests: List[Cell]            # List of cells
    system_test_cases: List[Tuple[str,str,Cell]] # List of (filename, content, original code cell)
    system_test_setting: Callable        # judge_setting.generate created from Python code

    def submission_redirection(self):
        m = re.match(r'#[ \t]*redirect-to[ \t]*:[ \t]*(\S+?\.ipynb)', self.student_code_cell.source)
        return m[1] if m else None

    def submission_cell(self):
        redirect_to = self.submission_redirection()
        if redirect_to:
            s = REDIRECTION_CELL_FORMAT.format(redirect_to=redirect_to)
        else:
            s = SUBMISSION_CELL_FORMAT.format(exercise_key=self.key, content=self.student_code_cell.source)
        return Cell(CellType.CODE, s)

    def submission_cell_filled(self):
        s = SUBMISSION_CELL_FORMAT.format(exercise_key=self.key, content=self.answer_examples[0].source if self.answer_examples else self.student_code_cell.source)
        return Cell(CellType.CODE, s)

    def generate_setting(self):
        params = {k: self.judge_parameters['override'].get(self.key, {}).get(k, v) for k, v in self.judge_parameters['default'].items()}
        return self.system_test_setting(params['environment'], params['time_limit'], params['memory_limit'], self.key, self.version, self.student_code_cell.source)

    @classmethod
    def load_judge_parameters(cls, json_path):
        with open(json_path, encoding='utf-8') as f:
            judge_env = json.load(f)
        params = ('environment', 'time_limit', 'memory_limit')
        cls.judge_parameters = {
            'default': {k: judge_env['default'][k] for k in params},
            'override': {ex_key: {k: d[k] for k in params if k in d} for ex_key, d in judge_env['override'].items()},
        }


def split_file_code_cell(cell: Cell):
    assert cell.cell_type == CellType.CODE
    lines = cell.source.strip().splitlines(keepends=True)
    first_line_regex = r'#+\s+([^/]{1,255})'
    match = re.fullmatch(first_line_regex, lines[0].strip())
    assert match is not None, f'RegExp pattern `{first_line_regex}` does not match the first line of a file code cell: {lines[0]}'
    return (match[1], ''.join(lines[1:]).strip() + '\n', cell)

def load_system_test_setting(cells: List[Cell]):
    exec(cells[0].source, {'generate_system_test_setting': judge_setting.generate_system_test_setting})
    return judge_setting.generate

def split_cells(raw_cells: Iterable[dict]):
    CONTENT_TYPE_REGEX = r'\*\*\*CONTENT_TYPE:\s*(.+?)\*\*\*'
    results = {}
    current_key = None
    cells = []
    for cell_type, source in ipynb_util.normalized_cells(raw_cells):
        logging.debug('[TRACE] %s %s', current_key, repr(source if len(source) <= 64 else source[:64] + ' ...'))
        if source.strip() == '':
            continue

        if cell_type in (CellType.CODE, CellType.RAW):
            assert current_key is not None
            cells.append(Cell(cell_type, source))
            continue
        assert cell_type == CellType.MARKDOWN

        matches = list(re.finditer(CONTENT_TYPE_REGEX, source))
        if len(matches) == 0:
            assert current_key is not None
            cells.append(Cell(cell_type, source))
            continue
        assert len(matches) == 1, f'Multiple field keys found in cell `{source}`.'

        if current_key is not None:
            results[current_key] = cells
            cells = []
        current_key = matches[0][1]

    results[current_key] = cells
    return results

def load_exercise(dirpath, exercise_key):
    raw_cells, metadata = ipynb_util.load_cells(os.path.join(dirpath, exercise_key + '.ipynb'))
    version = ipynb_metadata.master_metadata_version(metadata)
    exercise_kwargs = {'key': exercise_key, 'dirpath': dirpath, 'version': version}
    for field_key, cells in split_cells(raw_cells).items():
        field_enum = getattr(FieldKey, field_key)
        if field_enum in (FieldKey.WARNING, FieldKey.SYSTEM_TEST_CASES_EXECUTE_CELL):
            continue
        logging.debug(f'[TRACE] Validate field `{field_key}`')

        if (FieldProperty.OPTIONAL | FieldProperty.OPTIONAL) in field_enum.properties:
            pass
        elif (FieldProperty.OPTIONAL) in field_enum.properties:
            assert len(cells) <= 1, f'Field of `{field_key}` must have at most 1 cell but has {len(cells)}.'
        elif FieldProperty.LIST in field_enum.properties:
            assert len(cells) > 0, f'Field of `{field_key}` must not be empty.'
        elif FieldProperty.SINGLE in field_enum.properties:
            assert len(cells) == 1, f'Field of `{field_key}` must have 1 cell.'

        if FieldProperty.CODE in field_enum.properties:
            assert all(x.cell_type == CellType.CODE for x in cells), f'Field of `{field_key}` must have only code cell(s).'

        if len(cells) > 0 and FieldProperty.MARKDOWN_HEADED in field_enum.properties:
            assert cells[0].cell_type == CellType.MARKDOWN
            first_line_regex = r'#+\s+(.*)'
            first_line = cells[0].source.strip().splitlines()[0]
            m = re.fullmatch(first_line_regex, first_line)
            assert m is not None, f'The first content cell does not start with a heading in Markdown: `{first_line}`.'
            if field_enum == FieldKey.CONTENT:
                exercise_kwargs['title'] = m.groups()[0]

        exercise_kwargs[field_key.lower()] = {
            FieldKey.SYSTEM_TEST_SETTING: lambda: load_system_test_setting(cells),
            FieldKey.SYSTEM_TEST_CASES: lambda: [split_file_code_cell(x) for x in cells],
            FieldKey.STUDENT_CODE_CELL: lambda: cells[0],
        }.get(field_enum, lambda: cells)()

    return Exercise(**exercise_kwargs)

def cleanup_exercise_masters(exercises: Iterable[Exercise], commandline_options):
    deadlines_new = None
    if commandline_options.deadline:
        with open(commandline_options.deadline, encoding='utf-8') as f:
            deadlines_new = json.load(f)
    for exercise in exercises:
        filepath = os.path.join(exercise.dirpath, f'{exercise.key}.ipynb')
        cells, metadata = ipynb_util.load_cells(filepath, True)
        cells_new = [Cell(cell_type, source.strip()).to_ipynb() for cell_type, source in ipynb_util.normalized_cells(cells)]

        if deadlines_new is None:
            deadlines = ipynb_metadata.master_metadata_deadlines(metadata)
        else:
            logging.info(f'[INFO] Renew deadline of {exercise.key}')
            deadlines = deadlines_new

        if commandline_options.renew_version is not None:
            logging.info(f'[INFO] Renew version of {exercise.key}')
            if commandline_options.renew_version == hashlib.sha1:
                exercise_definition = {
                    'content': [x.to_ipynb() for x in exercise.content],
                    'submission_cell': exercise.submission_cell().to_ipynb(),
                    'student_tests': [x.to_ipynb() for x in exercise.student_tests],
                }
                m = hashlib.sha1()
                m.update(json.dumps(exercise_definition).encode())
                exercise.version = m.hexdigest()
            else:
                assert isinstance(commandline_options.renew_version, str)
                exercise.version = commandline_options.renew_version

        metadata_new = ipynb_metadata.master_metadata(exercise.key, True, exercise.version, exercise.title, deadlines)
        ipynb_util.save_as_notebook(filepath, cells_new, metadata_new)

def summarize_testcases(exercise: Exercise):
    contents = []
    is_not_decorator_line = lambda x: not x.startswith('@judge_util.')
    for _, src, _ in exercise.system_test_cases:
        contents.extend(filter(is_not_decorator_line, itertools.dropwhile(is_not_decorator_line, src.splitlines())))
        contents.append('')
    contents.pop()
    return Cell(CellType.CODE, '\n'.join(contents))

def create_exercise_configuration(exercise: Exercise):
    tests_dir = os.path.join(CONF_DIR, exercise.key)
    os.makedirs(tests_dir, exist_ok=True)

    cells = [x.to_ipynb() for x in itertools.chain(exercise.content, [exercise.student_code_cell])]
    _, metadata = ipynb_util.load_cells(os.path.join(exercise.dirpath, exercise.key + '.ipynb'), True)
    ipynb_util.save_as_notebook(os.path.join(CONF_DIR, exercise.key + '.ipynb'), cells, metadata)
    setting = exercise.generate_setting()
    with open(os.path.join(tests_dir, 'setting.json'), 'w', encoding='utf-8') as f:
        json.dump(setting, f, indent=1, ensure_ascii=False)
    for name, content, _ in exercise.system_test_cases:
        with open(os.path.join(tests_dir, name), 'w', encoding='utf-8', newline='\n') as f:
            f.write(content)

    for path in judge_setting.required_files(setting):
        dest = os.path.join(tests_dir, path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(os.path.join(exercise.dirpath, path), dest)

def create_configuration(exercises: Iterable[Exercise]):
    shutil.rmtree(CONF_DIR, ignore_errors=True)
    for exercise in exercises:
        logging.info(f'[INFO] Creating configuration for `{exercise.key}` ...')
        create_exercise_configuration(exercise)

    logging.info(f'[INFO] Creating configuration zip `{CONF_DIR}.zip` ...')
    with zipfile.ZipFile(CONF_DIR + '.zip', 'w', zipfile.ZIP_DEFLATED) as zipf:
        for dirpath, _, files in os.walk(CONF_DIR):
            arcdirpath = dirpath[len(os.path.join(CONF_DIR, '')):]
            for fname in files:
                zipf.write(os.path.join(dirpath, fname), os.path.join(arcdirpath, fname))

def create_bundled_forms(exercise_bundles):
    for dirpath, exercises in exercise_bundles.items():
        dirname = os.path.basename(dirpath)
        try:
            raw_cells, _ = ipynb_util.load_cells(os.path.join(dirpath, INTRODUCTION_FILE))
            intro = [Cell(t, s) for t, s in ipynb_util.normalized_cells(raw_cells)]
        except FileNotFoundError:
            intro = [Cell(CellType.MARKDOWN, f'# {os.path.basename(dirpath)}')]

        # Create answer
        body = []
        for exercise in exercises:
            body.extend(exercise.content)
            body.extend(exercise.answer_examples)
            body.append(summarize_testcases(exercise))
            body.extend(exercise.explanation)
        filepath = os.path.join(dirpath, f'ans_{dirname}.ipynb')
        metadata = ipynb_metadata.COMMON_METADATA
        ipynb_util.save_as_notebook(filepath, [c.to_ipynb() for c in itertools.chain(intro, body)], metadata)

        # Create form
        body = []
        for exercise in exercises:
            body.extend(exercise.content)
            body.append(exercise.submission_cell())
            body.extend(exercise.student_tests)
        filepath = os.path.join(dirpath, f'form_{dirname}.ipynb')
        key_to_ver = {ex.key: ex.version for ex in exercises if ex.submission_redirection() is None}
        metadata = ipynb_metadata.submission_metadata(key_to_ver, True)
        ipynb_util.save_as_notebook(filepath, [c.to_ipynb() for c in itertools.chain(intro, body)], metadata)

        for ex in exercises:
            if ex.submission_redirection():
                create_redirect_form(ex)

def create_single_forms(exercises: Iterable[Exercise]):
    for ex in exercises:
        # Create answer
        cells = itertools.chain(ex.content, ex.answer_examples, [summarize_testcases(ex)], ex.explanation)
        filepath = os.path.join(ex.dirpath, f'ans_{ex.key}.ipynb')
        metadata = ipynb_metadata.COMMON_METADATA
        ipynb_util.save_as_notebook(filepath, [c.to_ipynb() for c in cells], metadata)

        # Create form
        cells = itertools.chain(ex.content, [ex.submission_cell()], ex.student_tests)
        if ex.submission_redirection() is None:
            filepath = os.path.join(ex.dirpath, f'form_{ex.key}.ipynb')
            metadata =  ipynb_metadata.submission_metadata({ex.key: ex.version}, True)
        else:
            create_redirect_form(ex)
            filepath = os.path.join(ex.dirpath, f'pseudo-form_{ex.key}.ipynb')
            metadata = ipynb_metadata.COMMON_METADATA
        ipynb_util.save_as_notebook(filepath, [c.to_ipynb() for c in cells], metadata)

def create_redirect_form(exercise: Exercise):
    redirect_to = exercise.submission_redirection()
    filepath = os.path.join(exercise.dirpath, redirect_to)
    cells, _ = ipynb_util.load_cells(filepath, True)
    assert any(re.search(rf'<\[ {exercise.key} \]>', line)
               for c in cells if c['cell_type'] == CellType.CODE.value for line in c['source']), \
        f'{redirect_to} has no answer cell for {exercise.key}.'
    metadata = ipynb_metadata.submission_metadata({exercise.key: exercise.version}, True)
    ipynb_util.save_as_notebook(filepath, cells, metadata)

def create_filled_form(exercises: Iterable[Exercise], filepath):
    metadata =  ipynb_metadata.submission_metadata({ex.key: ex.version for ex in exercises}, True)
    ipynb_util.save_as_notebook(filepath, [ex.submission_cell_filled().to_ipynb() for ex in exercises], metadata)

def load_sources(source_paths: Iterable[str], *, master_loader=load_exercise):
    exercises = []
    bundles = collections.defaultdict(list)
    existing_keys = {}
    for path in sorted(source_paths):
        if os.path.isdir(path):
            dirpath = path
            dirname = os.path.basename(dirpath)
            logging.info(f'[INFO] Loading `{dirpath}`...')
            for nb in sorted(os.listdir(dirpath)):
                match = re.fullmatch(fr'({dirname}[-_].*)\.ipynb', nb)
                if match is None:
                    continue
                exercise_key = match.groups()[0]
                assert exercise_key not in existing_keys, \
                    f'[ERROR] Exercise key conflicts between `{dirpath}/{nb}` and `{existing_keys[exercise_key]}`.'
                existing_keys[exercise_key] = os.path.join(dirpath, nb)
                bundles[dirpath].append(master_loader(dirpath, exercise_key))
                logging.info(f'[INFO] Loaded `{dirpath}/{nb}`')
        else:
            filepath = path
            if not filepath.endswith('.ipynb'):
                logging.info(f'[INFO] Skip {filepath}')
                continue
            dirpath, filename = os.path.split(filepath)
            exercise_key, _ = os.path.splitext(filename)
            assert exercise_key not in existing_keys, \
                f'[ERROR] Exercise key conflicts between `{filepath}` and `{existing_keys[exercise_key]}`.'
            existing_keys[exercise_key] = filepath
            exercises.append(master_loader(dirpath, exercise_key))
            logging.info(f'[INFO] Loaded `{filepath}`')
    return exercises, bundles


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--verbose', action='store_true', help='Verbose option')
    parser.add_argument('-d', '--deadline', metavar='DEADLINE_JSON', help='Specify a JSON file of deadline settings.')
    parser.add_argument('-c', '--configuration', metavar='JUDGE_ENV_JSON', help='Create configuration with environmental parameters specified in JSON.')
    parser.add_argument('-n', '--renew_version', nargs='?', const=hashlib.sha1, metavar='VERSION', help='Renew the versions of every exercise (default: the SHA1 hash of each exercise definition)')
    parser.add_argument('-s', '--source', nargs='*', required=True, help=f'Specify source(s) (ipynb files in separate mode and directories in bundle mode)')
    parser.add_argument('-ff', '--filled_form', nargs='?', const='form_filled_all.ipynb', help='Generate an all-filled form (default: form_filled_all.ipynb)')
    commandline_options = parser.parse_args()
    if commandline_options.verbose:
        logging.getLogger().setLevel('DEBUG')
    else:
        logging.getLogger().setLevel('INFO')

    separates, bundles = load_sources(commandline_options.source)
    exercises = list(itertools.chain(*bundles.values(), separates))

    logging.info('[INFO] Cleaning up exercise masters...')
    cleanup_exercise_masters(exercises, commandline_options)

    logging.info('[INFO] Creating bundled forms...')
    create_bundled_forms(bundles)
    logging.info('[INFO] Creating separate forms...')
    create_single_forms(separates)

    if commandline_options.configuration:
        Exercise.load_judge_parameters(commandline_options.configuration)
        logging.info(f'[INFO] Creating configuration with `{repr(Exercise.judge_parameters)}` ...')
        create_configuration(exercises)

    if commandline_options.filled_form:
        logging.info(f'[INFO] Creating filled form `{commandline_options.filled_form}` ...')
        create_filled_form(exercises, commandline_options.filled_form)

if __name__ == '__main__':
    main()
