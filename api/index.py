"""
Codebase Visualizer — Backend API
Vercel Python serverless function (Flask)
"""
from flask import Flask, request, jsonify
import ast
import json
import os
import re
import math
import tempfile
import shutil
import zipfile
import io
from api.auth import auth_bp   # 👈 ADD THIS LINE
from pathlib import Path
from typing import Any

app = Flask(__name__)

app.register_blueprint(auth_bp)

try:
    import chardet
    HAS_CHARDET = True
except ImportError:
    HAS_CHARDET = False

try:
    import networkx as nx
    HAS_NX = True
except ImportError:
    HAS_NX = False

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    HAS_LIMITER = True
except ImportError:
    HAS_LIMITER = False

import urllib.request

app = Flask(__name__)

# Rate limiting (10 analyses/minute, 100/hour)
if HAS_LIMITER:
    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=["100 per hour", "10 per minute"],
        storage_uri="memory://",
    )
else:
    limiter = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS = {
    '.py': 'Python', '.js': 'JavaScript', '.jsx': 'JavaScript',
    '.ts': 'TypeScript', '.tsx': 'TypeScript', '.java': 'Java',
    '.go': 'Go', '.rs': 'Rust', '.cpp': 'C++', '.cc': 'C++',
    '.c': 'C', '.h': 'C', '.hpp': 'C++', '.cs': 'C#', '.rb': 'Ruby',
    '.php': 'PHP', '.swift': 'Swift', '.kt': 'Kotlin', '.scala': 'Scala',
    '.vue': 'Vue', '.svelte': 'Svelte', '.html': 'HTML', '.css': 'CSS',
    '.scss': 'SCSS', '.sass': 'SASS', '.json': 'JSON', '.yaml': 'YAML',
    '.yml': 'YAML', '.md': 'Markdown', '.sh': 'Shell', '.sql': 'SQL',
}

SKIP_DIRS = {
    'node_modules', '.git', '__pycache__', '.pytest_cache', 'venv', '.venv',
    'env', 'dist', 'build', '.next', '.nuxt', 'coverage', 'vendor',
    '.mypy_cache', '.ruff_cache', 'target', 'bin', 'obj', '.github',
    'docs', 'doc', 'examples', 'test', 'tests', 'spec', '__mocks__',
    'fixtures', 'assets', 'static', 'public', 'migrations', '.yarn',
}

MAX_FILES = 300          # max files to walk per repo
MAX_FILE_SIZE = 500_000  # 500 KB per file
MAX_REPO_MB = 150        # reject repos larger than 150 MB (GitHub reports in KB)
MAX_REPO_FILES = 10000   # reject repos with too many total files

# ---------------------------------------------------------------------------
# CORS helper
# ---------------------------------------------------------------------------

def cors_response(data=None, status=200):
    resp = jsonify(data) if data is not None else jsonify({})
    resp.status_code = status
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return resp

# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def parse_github_url(url: str):
    url = url.strip().rstrip('/')
    parts = url.replace('https://github.com/', '').replace('http://github.com/', '')
    segments = [s for s in parts.split('/') if s]
    if len(segments) < 2:
        raise ValueError(f"Invalid GitHub URL: {url}")
    owner, repo = segments[0], segments[1]
    if repo.endswith('.git'):
        repo = repo[:-4]
    branch = ''
    if len(segments) >= 4 and segments[2] == 'tree':
        branch = segments[3]
    return f"{owner}/{repo}", branch


def gh_request(url: str, timeout: int = 10) -> bytes:
    req = urllib.request.Request(url, headers={'User-Agent': 'CodebaseVisualizer/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def check_repo_limits(owner_repo: str):
    """Reject repos that are too large before downloading. Uses GitHub public API."""
    try:
        data = json.loads(gh_request(f"https://api.github.com/repos/{owner_repo}", timeout=8))
        size_kb = data.get('size', 0)
        size_mb = size_kb / 1024
        if size_mb > MAX_REPO_MB:
            raise ValueError(
                f"Repository is {size_mb:.0f} MB — too large to analyze (limit: {MAX_REPO_MB} MB). "
                f"Try a smaller repo or specify a branch with fewer files."
            )
    except ValueError:
        raise
    except Exception:
        pass  # If GitHub API fails, proceed anyway


def fetch_raw_file(owner_repo: str, branch: str, file_path: str) -> bytes:
    """Fetch a single file via GitHub raw content API — takes < 1s."""
    branches_to_try = [branch] if branch else []
    branches_to_try += ['main', 'master', 'HEAD']
    for b in dict.fromkeys(branches_to_try):  # deduplicate preserving order
        try:
            url = f"https://raw.githubusercontent.com/{owner_repo}/{b}/{file_path}"
            return gh_request(url, timeout=10), b
        except Exception:
            continue
    raise FileNotFoundError(f"Could not fetch {file_path} from {owner_repo}")


def fetch_repo_zip(owner_repo: str, branch: str = ''):
    branches_to_try = [branch] if branch else []
    branches_to_try += ['main', 'master']

    for b in dict.fromkeys(branches_to_try):
        if not b:
            continue
        try:
            data = gh_request(
                f"https://codeload.github.com/{owner_repo}/zip/refs/heads/{b}",
                timeout=55
            )
            return data, b
        except Exception:
            continue
    data = gh_request(f"https://codeload.github.com/{owner_repo}/zip/HEAD", timeout=55)
    return data, 'HEAD'


def extract_zip(zip_bytes: bytes, tmpdir: str) -> str:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall(tmpdir)
    dirs = [d for d in os.listdir(tmpdir) if os.path.isdir(os.path.join(tmpdir, d))]
    return os.path.join(tmpdir, dirs[0]) if dirs else tmpdir

# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------

def read_file_safe(path: str) -> str | None:
    try:
        raw = open(path, 'rb').read(MAX_FILE_SIZE)
        if HAS_CHARDET:
            enc = chardet.detect(raw).get('encoding') or 'utf-8'
        else:
            enc = 'utf-8'
        return raw.decode(enc, errors='replace')
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Language-specific import/export extraction
# ---------------------------------------------------------------------------

def extract_python_info(content: str, file_path: str):
    imports, exports, functions, classes = [], [], [], []
    complexity = 1
    try:
        tree = ast.parse(content, filename=file_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module.split('.')[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not any(isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef))
                           for p in ast.walk(tree) if p is not node and
                           any(c is node for c in ast.walk(p) if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)))):
                    pass
                func_info = {
                    'name': node.name,
                    'line': node.lineno,
                    'complexity': 1 + sum(
                        1 for n in ast.walk(node)
                        if isinstance(n, (ast.If, ast.For, ast.While, ast.Try,
                                          ast.ExceptHandler, ast.With, ast.Assert,
                                          ast.comprehension))
                    )
                }
                functions.append(func_info)
                complexity += func_info['complexity'] - 1
            elif isinstance(node, ast.ClassDef):
                classes.append({'name': node.name, 'line': node.lineno, 'methods': [
                    m.name for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]})
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                exports.append(node.id)
    except SyntaxError:
        pass
    return {
        'imports': list(dict.fromkeys(imports)),
        'exports': list(dict.fromkeys(exports[:20])),
        'functions': functions[:30],
        'classes': classes[:20],
        'complexity': min(complexity, 50),
    }


def extract_js_ts_info(content: str):
    imports, exports, functions, classes = [], [], [], []
    import_patterns = [
        r"import\s+.*?from\s+['\"]([^'\"]+)['\"]",
        r"require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
        r"import\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
    ]
    for pat in import_patterns:
        for m in re.finditer(pat, content, re.MULTILINE):
            imports.append(m.group(1))
    for m in re.finditer(r'export\s+(?:default\s+)?(?:function|class|const|let|var)\s+(\w+)', content):
        exports.append(m.group(1))
    for m in re.finditer(r'(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\()', content):
        name = m.group(1) or m.group(2)
        if name:
            functions.append({'name': name, 'line': content[:m.start()].count('\n') + 1, 'complexity': 1})
    for m in re.finditer(r'class\s+(\w+)', content):
        classes.append({'name': m.group(1), 'line': content[:m.start()].count('\n') + 1, 'methods': []})
    branches = len(re.findall(r'\b(?:if|else|for|while|switch|catch|&&|\|\||\?)\b', content))
    complexity = max(1, min(1 + branches // 3, 50))
    return {
        'imports': list(dict.fromkeys(imports)),
        'exports': list(dict.fromkeys(exports[:20])),
        'functions': functions[:30],
        'classes': classes[:20],
        'complexity': complexity,
    }


def extract_generic_info(content: str):
    return {'imports': [], 'exports': [], 'functions': [], 'classes': [], 'complexity': 1}

# ---------------------------------------------------------------------------
# File analysis
# ---------------------------------------------------------------------------

def analyze_file(file_path: str, root_dir: str):
    ext = Path(file_path).suffix.lower()
    language = SUPPORTED_EXTENSIONS.get(ext)
    if not language:
        return None
    content = read_file_safe(file_path)
    if content is None:
        return None
    stat = os.stat(file_path)
    lines = content.count('\n') + 1
    rel = os.path.relpath(file_path, root_dir).replace('\\', '/')
    if language == 'Python':
        info = extract_python_info(content, file_path)
    elif language in ('JavaScript', 'TypeScript'):
        info = extract_js_ts_info(content)
    else:
        info = extract_generic_info(content)
    return {
        'filePath': rel,
        'language': language,
        'linesOfCode': lines,
        'size': stat.st_size,
        'complexity': info['complexity'],
        'imports': info['imports'],
        'exports': info['exports'],
        'functions': info['functions'],
        'classes': info['classes'],
        '_content': content,
    }

# ---------------------------------------------------------------------------
# Graph + cycles
# ---------------------------------------------------------------------------

def build_graph(files: list[dict]):
    file_index = {f['filePath'] for f in files}
    nodes, edges = [], []
    adj: dict[str, list[str]] = {}
    for f in files:
        nodes.append({'id': f['filePath'], 'language': f['language'],
                      'linesOfCode': f['linesOfCode'], 'complexity': f['complexity'],
                      'size': f['size']})
        adj[f['filePath']] = []

    for f in files:
        src = f['filePath']
        src_dir = os.path.dirname(src)
        for imp in f['imports']:
            # Resolve relative imports
            if imp.startswith('.'):
                base = os.path.normpath(os.path.join(src_dir, imp)).replace('\\', '/')
                candidates = [base, base + '.py', base + '.ts', base + '.tsx',
                              base + '.js', base + '.jsx', base + '/index.ts',
                              base + '/index.js', base + '/index.py']
                for c in candidates:
                    if c in file_index:
                        edges.append({'source': src, 'target': c})
                        adj[src].append(c)
                        break
            else:
                # Try matching by module name
                mod = imp.replace('.', '/').split('/')[0]
                for fpath in file_index:
                    stem = Path(fpath).stem
                    parts = fpath.split('/')
                    if stem == mod or parts[0] == mod:
                        if fpath != src:
                            edges.append({'source': src, 'target': fpath})
                            adj[src].append(fpath)
                            break

    circular: list[list[str]] = []
    if HAS_NX:
        G = nx.DiGraph()
        G.add_nodes_from(f['filePath'] for f in files)
        for e in edges:
            G.add_edge(e['source'], e['target'])
        try:
            cycles = list(nx.simple_cycles(G))
            circular = [c for c in cycles if len(c) >= 2][:20]
        except Exception:
            pass
    else:
        # Simple DFS cycle detection fallback
        visited: set[str] = set()
        rec_stack: set[str] = set()

        def dfs(node):
            visited.add(node)
            rec_stack.add(node)
            for nb in adj.get(node, []):
                if nb not in visited:
                    cycle = dfs(nb)
                    if cycle:
                        return cycle
                elif nb in rec_stack:
                    return [node, nb]
            rec_stack.discard(node)
            return None

        for n in list(adj.keys()):
            if n not in visited:
                dfs(n)

    return {'nodes': nodes, 'edges': edges, 'circularDependencies': circular}


def compute_summary(files: list[dict], graph: dict, owner_repo: str):
    total_files = len(files)
    total_lines = sum(f['linesOfCode'] for f in files)
    total_size = sum(f['size'] for f in files)
    avg_complexity = sum(f['complexity'] for f in files) / max(total_files, 1)

    lang_counts: dict[str, dict] = {}
    for f in files:
        l = f['language']
        if l not in lang_counts:
            lang_counts[l] = {'files': 0, 'lines': 0}
        lang_counts[l]['files'] += 1
        lang_counts[l]['lines'] += f['linesOfCode']

    languages = sorted([
        {'language': l, 'files': v['files'], 'lines': v['lines'],
         'percentage': v['files'] / total_files * 100}
        for l, v in lang_counts.items()
    ], key=lambda x: -x['files'])

    circ_count = len(graph['circularDependencies'])
    high_complexity = sum(1 for f in files if f['complexity'] > 10)

    score = 100
    score -= min(circ_count * 10, 30)
    score -= min(high_complexity * 2, 20)
    if avg_complexity > 5:
        score -= min(int((avg_complexity - 5) * 3), 20)
    score -= min(max(total_files - 50, 0) // 10, 10)
    score = max(0, min(100, score))

    return {
        'repoName': owner_repo.split('/')[-1] if '/' in owner_repo else owner_repo,
        'totalFiles': total_files,
        'totalLines': total_lines,
        'totalSize': total_size,
        'avgComplexity': round(avg_complexity, 2),
        'languages': languages,
        'circularDependencies': circ_count,
        'highComplexityFiles': high_complexity,
        'healthScore': score,
    }

# ---------------------------------------------------------------------------
# Core: analyze a repo
# ---------------------------------------------------------------------------

def analyze_repo(repo_url: str, branch: str = '', file_path: str = ''):
    owner_repo, detected_branch = parse_github_url(repo_url)
    branch = branch or detected_branch

    # Pre-flight: reject repos that are too large before we touch the zip
    check_repo_limits(owner_repo)

    tmpdir = tempfile.mkdtemp()
    try:
        zip_bytes, used_branch = fetch_repo_zip(owner_repo, branch)
        root = extract_zip(zip_bytes, tmpdir)

        files_data = []
        file_count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fname in filenames:
                if file_count >= MAX_FILES:
                    break
                fpath = os.path.join(dirpath, fname)
                if os.path.getsize(fpath) > MAX_FILE_SIZE:
                    continue
                info = analyze_file(fpath, root)
                if info:
                    files_data.append(info)
                    file_count += 1

        graph = build_graph(files_data)
        summary = compute_summary(files_data, graph, owner_repo)

        # Strip _content from graph nodes before returning
        files_no_content = [{k: v for k, v in f.items() if k != '_content'} for f in files_data]

        if file_path:
            norm_path = file_path.replace('\\', '/')
            found = None
            circ_files = set()
            for chain in graph['circularDependencies']:
                circ_files.update(chain)

            for f in files_data:
                if f['filePath'] == norm_path or f['filePath'].endswith(norm_path):
                    found = {
                        'filePath': f['filePath'],
                        'language': f['language'],
                        'linesOfCode': f['linesOfCode'],
                        'size': f['size'],
                        'complexity': f['complexity'],
                        'imports': f['imports'],
                        'exports': f['exports'],
                        'functions': f['functions'],
                        'classes': f['classes'],
                        'isCircular': f['filePath'] in circ_files,
                        'content': f['_content'] or '',
                    }
                    break

            if not found:
                return {'error': f'File not found: {file_path}'}
            return {'action': 'file_details', 'result': found}

        return {
            'action': 'analyze',
            'result': {
                'graph': graph,
                'summary': summary,
            }
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.before_request
def handle_options():
    if request.method == 'OPTIONS':
        resp = app.make_default_options_response()
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp


@app.after_request
def add_cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return resp


@app.route('/api/healthz', methods=['GET'])
def healthz():
    return jsonify({'status': 'ok'})


@app.route('/api/analysis/analyze', methods=['POST'])
def route_analyze():
    body = request.get_json(silent=True) or {}
    repo_url = body.get('repoUrl', '')
    branch = body.get('branch') or ''
    if not repo_url:
        return jsonify({'error': 'repoUrl is required'}), 400
    try:
        output = analyze_repo(repo_url, branch)
        if output.get('error'):
            return jsonify({'error': output['error']}), 422
        return jsonify(output.get('result', output))
    except Exception as e:
        return jsonify({'error': str(e)}), 422


@app.route('/api/analysis/graph', methods=['POST'])
def route_graph():
    body = request.get_json(silent=True) or {}
    repo_url = body.get('repoUrl', '')
    branch = body.get('branch') or ''
    if not repo_url:
        return jsonify({'error': 'repoUrl is required'}), 400
    try:
        output = analyze_repo(repo_url, branch)
        if output.get('error'):
            return jsonify({'error': output['error']}), 422
        result = output.get('result', {})
        return jsonify(result.get('graph', result))
    except Exception as e:
        return jsonify({'error': str(e)}), 422


@app.route('/api/analysis/file', methods=['POST'])
def route_file():
    """
    Fast file detail endpoint — fetches a single file via GitHub raw content API
    (< 1 second) instead of re-downloading the entire repository zip.
    """
    body = request.get_json(silent=True) or {}
    repo_url = body.get('repoUrl', '')
    branch = body.get('branch') or ''
    file_path = body.get('filePath', '')
    # isCircular is determined by the frontend from cached graph data
    is_circular = bool(body.get('isCircular', False))

    if not repo_url or not file_path:
        return jsonify({'error': 'repoUrl and filePath are required'}), 400

    try:
        owner_repo, detected_branch = parse_github_url(repo_url)
        branch = branch or detected_branch

        # Fetch just this one file — takes < 1 second via GitHub raw API
        content_bytes, used_branch = fetch_raw_file(owner_repo, branch, file_path)

        # Decode
        if HAS_CHARDET:
            enc = chardet.detect(content_bytes).get('encoding') or 'utf-8'
        else:
            enc = 'utf-8'
        content = content_bytes.decode(enc, errors='replace')

        # Truncate very large files for display
        if len(content) > MAX_FILE_SIZE:
            content = content[:MAX_FILE_SIZE] + '\n\n# [file truncated — too large to display fully]'

        # Determine language
        ext = ('.' + file_path.rsplit('.', 1)[-1].lower()) if '.' in file_path else ''
        language = SUPPORTED_EXTENSIONS.get(ext, 'Text')

        # AST / regex analysis on just this file
        if language == 'Python':
            info = extract_python_info(content, file_path)
        elif language in ('JavaScript', 'TypeScript'):
            info = extract_js_ts_info(content)
        else:
            info = extract_generic_info(content)

        lines = content.count('\n') + 1

        return jsonify({
            'filePath': file_path,
            'language': language,
            'linesOfCode': lines,
            'size': len(content_bytes),
            'complexity': info['complexity'],
            'imports': info['imports'],
            'exports': info['exports'],
            'functions': info['functions'],
            'classes': info['classes'],
            'isCircular': is_circular,
            'content': content,
        })

    except FileNotFoundError as e:
        return jsonify({'error': str(e)}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 422


@app.route('/api/analysis/summary', methods=['POST'])
def route_summary():
    body = request.get_json(silent=True) or {}
    repo_url = body.get('repoUrl', '')
    branch = body.get('branch') or ''
    if not repo_url:
        return jsonify({'error': 'repoUrl is required'}), 400
    try:
        output = analyze_repo(repo_url, branch)
        if output.get('error'):
            return jsonify({'error': output['error']}), 422
        result = output.get('result', {})
        return jsonify(result.get('summary', result))
    except Exception as e:
        return jsonify({'error': str(e)}), 422
