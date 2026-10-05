#!/usr/bin/python

from ansible.module_utils.basic import AnsibleModule
from dataclasses import dataclass, field
from pathlib import Path
import json, shutil, yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from jinja2_ansible_filters import AnsibleCoreFiltersExtension

# Precedence: base values → dsc overrides → forced overrides (Argo CD only)

# Kubernetes patchMergeKey per list field; a tuple lists candidate keys in priority order
LIST_MERGE_KEYS = {
    'env': 'name',
    'envVars': 'name',
    'envFrom': ('secretRef.name', 'configMapRef.name'),
    'volumes': 'name',
    'volumeMounts': 'mountPath',
    'containers': 'name',
    'initContainers': 'name',
    'imagePullSecrets': 'name',
    'ports': ('containerPort', 'port'),
}

# Rendering state ============================================================
@dataclass
class RenderContext:
    app_name: str
    env_name: str
    dsc: dict
    role_path: Path
    gitops_local_repo: Path
    source_path: Path
    destination_path: Path
    role_vars: dict
    jinja_context: dict
    changed: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

def create_render_context(env_tuple, dsc, role_path, gitops_local_repo, context):
    env_name, app_config = env_tuple
    app_name = app_config["argocd_app"]
    role_path, gitops_local_repo = Path(role_path), Path(gitops_local_repo)
    repo_path = dsc.get("global", {}).get("gitOps", {}).get("repo", {}).get("path", "")
    source_path = role_path / "templates" / app_name
    destination_path = gitops_local_repo / repo_path / "envs" / env_name / "apps" / app_name
    # Ansible-equivalent globals, so templates render as they would under Ansible
    jinja_context = {**context, "dsc": dsc, "env": env_name}
    role_vars_path = role_path / "vars" / "main.yaml"
    role_vars = (yaml.safe_load(role_vars_path.read_text()) or {}) if role_vars_path.exists() else {}
    return RenderContext(app_name=app_name, env_name=env_name, dsc=dsc, role_path=role_path, gitops_local_repo=gitops_local_repo,
        destination_path=destination_path, source_path=source_path, role_vars=role_vars, jinja_context=jinja_context)

# YAML helpers ===============================================================
class UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError("while constructing a mapping", node.start_mark, f"found duplicate key: {key!r}", key_node.start_mark)
            seen.add(key)
        return super().construct_mapping(node, deep)

def load_yaml_unique(content):
    "yaml.safe_load that raises on duplicate keys instead of silently keeping the last one"
    return yaml.load(content, Loader=UniqueKeyLoader)

class LiteralStr(str):
    "A string that must use block scalar style even when it has no newline"

class NiceDumper(yaml.SafeDumper):
    pass

def literal_representer(dumper, data):
    style = '|' if isinstance(data, LiteralStr) or '\n' in data else None
    return dumper.represent_scalar('tag:yaml.org,2002:str', data, style=style)

NiceDumper.add_representer(str, literal_representer)
NiceDumper.add_representer(LiteralStr, literal_representer)

def ansible_to_nice_yaml(data, stream=None, **kwds):
    "Like Ansible's to_nice_yaml: multiline strings use block scalars"
    return yaml.dump(data, stream, Dumper=NiceDumper, default_flow_style=False, sort_keys=True, **kwds)

def write_with_newline(path, content):
    "Write content with a trailing newline; return True when the file changed"
    if not content:
        return False
    if not content.endswith('\n'): content += '\n'
    if path.exists() and path.read_text(encoding='utf-8') == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    return True

# Jinja helpers ==============================================================
def ansible_dict2items(value, key_name='key', value_name='value'):
    if not isinstance(value, dict):
        return []
    return [{key_name: key, value_name: value} for key, value in value.items()]

def create_jinja_env(search_paths, trim=True):
    "Jinja env equivalent to Ansible's template lookup, including filters (jinja2-ansible-filters + dict2items + combine)"
    loader = FileSystemLoader(search_paths)
    env = Environment(loader=loader, extensions=[AnsibleCoreFiltersExtension], undefined=StrictUndefined, trim_blocks=trim, lstrip_blocks=trim)
    env.filters['dict2items'] = ansible_dict2items
    env.filters['combine'] = deep_merge
    return env

# Generic merge helpers ======================================================
def deep_merge(base, over):
    if not isinstance(base, dict) or not isinstance(over, dict):
        return over
    res = base.copy()
    for key, value in over.items():
        if isinstance(value, dict) and key in res and isinstance(res[key], dict):
            res[key] = deep_merge(res[key], value)
        else:
            res[key] = value
    return res

def _merge_key(item, field):
    "First usable merge key of a list item, as (dotted path, value), or None"
    if not isinstance(item, dict):
        return None
    keys = LIST_MERGE_KEYS.get(field, ())
    for dotted in (keys,) if isinstance(keys, str) else keys:
        value = item
        for part in dotted.split('.'):
            value = value.get(part) if isinstance(value, dict) else None
        if value not in (None, ''):
            return dotted, value
    return None

def _merge_key_label(key):
    return f"{key[0].split('.')[0]}={key[1]}"

def merge_list(ctx, base_list, over_list, path):
    "Merge by patchMergeKey; replace unknown fields and report items without a key"
    field = path[-1] if path else ''
    if field.startswith('extra') and len(field) > len('extra'):
        field = field[len('extra'):]
        field = field[0].lower() + field[1:]

    if field not in LIST_MERGE_KEYS:
        if base_list:
            ctx.warnings.append(f"List at '{'.'.join(path)}' fully replaced by dsc override (no known merge key for this field)")
        return over_list

    keys = [_merge_key(item, field) for item in base_list + over_list]
    if None in keys:
        ctx.errors.append(f"List at '{'.'.join(path)}' contains an item without a usable merge key for field '{field}'")
        return over_list

    result = [dict(item) for item in base_list]
    index = {key: i for i, key in enumerate(keys[:len(base_list)])}
    for item, key in zip(over_list, keys[len(base_list):]):
        if key in index:
            i = index[key]
            result[i] = deep_merge_with_warnings(ctx, result[i], item, path + [f"[{_merge_key_label(key)}]"])
        else:
            result.append(item)
            ctx.warnings.append(f"List at '{'.'.join(path)}' gained new entry '{_merge_key_label(key)}' from dsc override")
    return result

def deep_merge_with_warnings(ctx, base, over, path):
    "Like deep_merge, but list-aware and warns when a dsc override replaces a role value"
    if isinstance(base, list) and isinstance(over, list):
        return merge_list(ctx, base, over, path)
    if not isinstance(base, dict) or not isinstance(over, dict):
        if base is not None and base != over:
            ctx.warnings.append(f"dsc override at '{'.'.join(path)}' replaces role default")
        return over
    result = base.copy()
    for key, value in over.items():
        new_path = path + [key]
        if key in result:
            result[key] = deep_merge_with_warnings(ctx, result[key], value, new_path)
        else:
            result[key] = value
    return result

# Small helpers ===============================================================
def get_path(d, *keys, default=None):
    "dict.get() through a chain of keys, defaulting cleanly if any level is missing or not a dict"
    for key in keys:
        if not isinstance(d, dict) or (d := d.get(key)) is None:
            return default
    return d

def setdefault_path(d, *keys):
    "dict.setdefault() through a chain of keys, creating {} at each missing level"
    for key in keys:
        d = d.setdefault(key, {})
    return d

def exc_str(e):
    "Render an exception as `<ClassName>: <message>`, for ctx.errors/warnings entries"
    return f"{type(e).__name__}: {e}"

# Argo CD helpers ============================================================
def normalize_ca_list(raw_ca):
    "Normalize a PEM string or list into a list of trimmed certificates"
    if not raw_ca:
        return []
    if isinstance(raw_ca, str):
        raw_ca = [raw_ca]
    return [ca.strip() for ca in raw_ca if ca and ca.strip()]

def apply_argocd_root_ca(ctx, frag_env, final_values, raw_root_ca):
    "Merge the role CA with dsc.argocd.values.oidcRootCA"
    role_ca = ''
    # Legacy guard: no role placeholder when no private CA is exposed
    if get_path(ctx.dsc, 'exposedCA', 'type', default='none') != 'none':
        rendered = frag_env.from_string(raw_root_ca).render(**ctx.jinja_context)
        role_ca = ((load_yaml_unique(rendered) or {}).get('rootCA') or '').rstrip('\n')

    # Renderer-only: get_user_overrides() copies dsc.argocd.values here
    user_cas = normalize_ca_list(final_values.get('argocd', {}).pop('oidcRootCA', None))
    if any('PRIVATE KEY' in ca for ca in user_cas):
        ctx.errors.append("dsc.argocd.values.oidcRootCA must contain certificates, not private keys")
        user_cas = [ca for ca in user_cas if 'PRIVATE KEY' not in ca]

    combined = '\n'.join(filter(None, [role_ca, *user_cas]))
    if not combined:
        return
    if user_cas:
        ctx.warnings.append(f"Appended {len(user_cas)} CA(s) from dsc.argocd.values.oidcRootCA")

    cm = setdefault_path(final_values, 'argocd', 'configs', 'cm')
    oidc = cm.get('oidc.config', '')
    oidc = (load_yaml_unique(oidc) if isinstance(oidc, str) else oidc) or {}
    if 'rootCA' in oidc and oidc['rootCA'] != combined:
        ctx.warnings.append("dsc override set oidc.config.rootCA directly; use dsc.argocd.values.oidcRootCA instead")
    oidc['rootCA'] = LiteralStr(combined)
    cm['oidc.config'] = ansible_to_nice_yaml(oidc).strip()

def apply_argocd_metrics_labels(ctx, final_values):
    additional_labels = ctx.dsc['global']['metrics']['additionalLabels']
    application_labels = setdefault_path(final_values, 'argocd', 'controller', 'metrics', 'applicationLabels')
    application_labels['enabled'] = True
    application_labels['labels'] = additional_labels

def apply_argocd_forced_overrides(ctx, frag_env, final_values):
    "Apply the Argo CD overrides that are not plain values merges"
    raw_root_ca = get_path(ctx.role_vars, 'values', 'argocd', 'rootCA_block', default='')
    try:
        if not raw_root_ca:
            raise ValueError("rootCA_block missing in role_vars")
        apply_argocd_root_ca(ctx, frag_env, final_values, raw_root_ca)
    except Exception as e:
        ctx.errors.append(f"rootCA merge error: {exc_str(e)}")

    try:
        if get_path(ctx.dsc, 'global', 'metrics', 'additionalLabels'):
            apply_argocd_metrics_labels(ctx, final_values)
    except Exception as e:
        ctx.errors.append(f"Metrics labels merge error: {exc_str(e)}")

# App configuration ==========================================================
def is_app_enabled(dsc, app_name):
    # observability's install flag lives under dsc.observatorium
    key = "observatorium" if app_name == "observability" else app_name
    return get_path(dsc, key, "installEnabled", default=False)

def get_user_overrides(ctx):
    "Return the dsc values that apply to this application"
    if ctx.app_name == 'gitlab' and 'post_conf_job' not in ctx.jinja_context:
        app_values = get_path(ctx.dsc, 'gitlabOperator', 'values', default={})
        cnpg_values = get_path(ctx.dsc, 'gitlab', 'cnpg', 'values', default={})
    elif ctx.app_name == 'cloudnativepg':
        app_values = get_path(ctx.dsc, 'cloudnativepg', 'operator', 'values', default={})
        cnpg_values = {}
    else:
        app_values = get_path(ctx.dsc, ctx.app_name, 'values', default={})
        cnpg_values = get_path(ctx.dsc, ctx.app_name, 'cnpg', 'values', default={})

    user_overrides = {}
    if app_values:
        user_overrides[ctx.app_name] = app_values
    if cnpg_values:
        user_overrides['cluster'] = cnpg_values
    cpn_values = get_path(ctx.dsc, 'cpnAnsibleJob', 'values')
    if cpn_values is not None:
        if (ctx.source_path / 'values' / '200-ansible-job.j2').exists():
            user_overrides['cpn-ansible-job'] = cpn_values
        elif get_path(ctx.dsc, ctx.app_name, 'repoSocle', default={}):
            ctx.errors.append(f"`cpnAnsibleJob` values provided in dsc but `200-ansible-job.j2` missing in {ctx.source_path}/values")
    return user_overrides

# Rendering phases ===========================================================
def render_static_files(ctx):
    files_src = ctx.source_path / "files"
    for src in (files_src.rglob('*') if files_src.exists() else []):
        if not src.is_file():
            continue
        dest = ctx.destination_path / src.relative_to(files_src)
        if not dest.exists() or dest.read_bytes() != src.read_bytes():
            ctx.changed = True
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

def render_templates(ctx):
    env_jinja = create_jinja_env([str(ctx.source_path), str(ctx.source_path / "templates")], trim=True)
    try:
        chart = load_yaml_unique(env_jinja.get_template("Chart.yaml.j2").render(**ctx.jinja_context))
        ctx.changed |= write_with_newline(ctx.destination_path / "Chart.yaml", ansible_to_nice_yaml(chart))
    except Exception as e:
        ctx.errors.append(f"Chart.yaml error: {exc_str(e)}")

    templates_dest = ctx.destination_path / "templates"
    templates_dest.mkdir(exist_ok=True)

    if ctx.app_name == 'gitlab':
        try: render_gitlab_definition(ctx, templates_dest)
        except Exception as e:
            ctx.errors.append(f"GitLab definition error: {exc_str(e)}")

    templates_src = ctx.source_path / "templates"
    if not templates_src.exists():
        # cloudnativepg and glexporter have no extra manifests
        if ctx.app_name not in ('cloudnativepg', 'glexporter'):
            ctx.warnings.append(f"Templates directory missing for {ctx.app_name} at {templates_src}")
        return

    current_dest_files = {f.name for f in templates_dest.iterdir() if f.is_file()}
    source_files = {f.name.removesuffix(".j2") for f in templates_src.glob("*.j2")}
    whitelist = {"gitlab.yaml"}  # GitLab CR: synthesized by render_gitlab_definition(), not from a *.j2 source
    for stale_file in (current_dest_files - source_files - whitelist):
        (templates_dest / stale_file).unlink()
        ctx.changed = True

    for tmpl_file in templates_src.glob("*.j2"):
        # users.yaml is maintained by hand in the GitOps repo
        if tmpl_file.name == "users.yaml.j2" and (templates_dest / "users.yaml").exists():
            continue
        try:
            rendered = env_jinja.get_template(tmpl_file.name).render(**ctx.jinja_context)
            dest_file = templates_dest / tmpl_file.name.removesuffix(".j2")
            if not rendered or rendered.strip() in ('', 'None'):
                if dest_file.exists():
                    dest_file.unlink()
                    ctx.changed = True
                continue
            ctx.changed |= write_with_newline(dest_file, rendered)
        except Exception as e:
            ctx.errors.append(f"Template {tmpl_file.name} error: {exc_str(e)}")

def load_base_values(ctx, values_src=None):
    "Load and merge values/*.j2 from values_src, or from the app's values/ dir"
    final_values = {}
    values_src = values_src or ctx.source_path / "values"
    if not values_src.exists():
        ctx.warnings.append(f"Base values directory missing for {ctx.app_name} at {values_src}")
        return final_values
    val_env = create_jinja_env([str(values_src)], trim=True)
    for val_file in sorted(values_src.glob("*.j2")):
        try:
            parsed = load_yaml_unique(val_env.get_template(val_file.name).render(**ctx.jinja_context))
            if parsed is None:
                continue
            if not isinstance(parsed, dict):
                ctx.errors.append(f"Base value {val_file.name} must contain a YAML mapping")
                continue
            final_values = deep_merge(final_values, parsed)
        except Exception as e:
            ctx.errors.append(f"Base value {val_file.name} error: {exc_str(e)}")
    return final_values

def render_gitlab_definition(ctx, templates_dest):
    "Write the gitlab-operator GitLab CR (separate from dsc.gitlabOperator chart values)"
    base_values = load_base_values(ctx, ctx.role_path / "gitlab" / "values")
    gitlab_values = deep_merge_with_warnings(ctx, base_values, get_path(ctx.dsc, 'gitlab', 'values', default={}), ['gitlab', 'values'])
    gitlab_definition = {
        "apiVersion": "apps.gitlab.com/v1beta1",
        "kind": "GitLab",
        "metadata": {
            "name": "gitlab",
            "namespace": ctx.dsc['gitlab']['namespace'],
            "annotations": {"argocd.argoproj.io/sync-wave": "5"},
        },
        "spec": {"chart": {"version": ctx.dsc['gitlab']['chartVersion'], "values": gitlab_values}},
    }
    ctx.changed |= write_with_newline(templates_dest / "gitlab.yaml", ansible_to_nice_yaml(gitlab_definition))

def render_values(ctx):
    "Base values → dsc overrides → forced renderer overrides"
    final_values = load_base_values(ctx)
    final_values = deep_merge_with_warnings(ctx, final_values, get_user_overrides(ctx), [])
    if ctx.app_name == 'argocd':
        frag_env = create_jinja_env([str(ctx.source_path)], trim=False)
        apply_argocd_forced_overrides(ctx, frag_env, final_values)
    ctx.changed |= write_with_newline(ctx.destination_path / "values.yaml", ansible_to_nice_yaml(final_values))

# Rendering orchestration ====================================================
def render_app(env_tuple, dsc, role_path, gitops_local_repo, context):
    "Render one env/app pair; return (has_errors, changed, message)"
    ctx = create_render_context(env_tuple, dsc, role_path, gitops_local_repo, context)
    if not is_app_enabled(ctx.dsc, ctx.app_name):
        return (False, False, f"Skipped {ctx.app_name} in {ctx.env_name}")
    ctx.destination_path.mkdir(parents=True, exist_ok=True)

    render_static_files(ctx)
    render_templates(ctx)
    try: render_values(ctx)
    except Exception as e:
        ctx.errors.append(f"Values rendering error: {exc_str(e)}")

    parts = [f"Rendered {ctx.app_name} in {ctx.env_name}"]
    if ctx.errors:
        parts.append("\nERROR: " + "\nERROR: ".join(ctx.errors))
    if ctx.warnings:
        parts.append("\nWARNING: " + "\nWARNING: ".join(ctx.warnings))
    return (bool(ctx.errors), ctx.changed, " ".join(parts))

def render_dsc_json(dsc, dsc_name, env, gitops_local_repo):
    "Write the per-env <dsc_name>.json"
    env_name = env['name']
    apps = [{
        "app": app['argocd_app'],
        "enabled": is_app_enabled(dsc, app['argocd_app']),
        "clusterName": app.get('clusterName', ''),
        "namespace": app['nameSpace'],
        "customNamespacePrefix": app.get('customNamespacePrefix', ''),
        "syncWave": int(app.get('syncWave', 10)),
    } for app in env.get('apps', [])]
    json_data = {
        "env": env_name,
        "provider": "self-hosted",
        "region": "fr-par",
        "namespacePrefix": dsc['global']['gitOps']['namespacePrefix'],
        "destination": {"clusterName": get_path(dsc, 'global', 'gitOps', 'envName', default='')},
        "targetRevision": get_path(dsc, 'global', 'gitOps', 'repo', 'revision', default='main'),
        "apps": apps,
    }
    repo_path = get_path(dsc, 'global', 'gitOps', 'repo', 'path', default='')
    dest = Path(gitops_local_repo) / repo_path / "envs" / env_name / f"{dsc_name}.json"
    changed = write_with_newline(dest, json.dumps(json_data, indent=2))
    return changed, f"Rendered dsc.json for {env_name}"

def render_all(dsc, envs, role_path, gitops_local_repo, context):
    "Render every env/app, plus dsc.json for a normal install; return (changed, results, has_errors)"
    tasks = [(env['name'], app) for env in envs for app in env.get('apps', [])]
    has_errors, changed, results = False, False, []

    # Per-env `<dsc_name>.json`
    if 'post_conf_job' not in context:
        for env in envs:
            try: env_changed, result = render_dsc_json(dsc, context['dsc_name'], env, gitops_local_repo)
            except Exception as e:
                has_errors = True
                env_changed = False
                result = f"ERROR: {context['dsc_name']}.json render failed for {env['name']}: {exc_str(e)}"
            results.append(result)
            changed |= env_changed

    for task in tasks:
        try: has_error, task_changed, result = render_app(task, dsc, role_path, gitops_local_repo, context)
        except Exception as e:
            has_error = True
            task_changed = False
            result = f"ERROR: render failed: {exc_str(e)}"
        results.append(result)
        has_errors |= has_error
        changed |= task_changed

    return changed, results, has_errors

# Ansible module entry point =================================================
def main():
    # envs holds generated credentials and context holds all hostvars; no_log keeps both out of -vvv output
    module = AnsibleModule(
        argument_spec=dict(
            dsc=dict(type='dict', required=True),
            envs=dict(type='list', required=True, no_log=True),
            role_path=dict(type='path', required=True),
            gitops_local_repo=dict(type='path', required=True),
            context=dict(type='dict', default={}, no_log=True),
        )
    )
    p = module.params
    changed, results, has_errors = render_all(p['dsc'], p['envs'], p['role_path'], p['gitops_local_repo'], p['context'])
    # exit_json, not fail_json, so the tasks file can pretty-print the summary first
    module.exit_json(changed=changed, msg=results, has_errors=has_errors)

if __name__ == '__main__':
    main()
