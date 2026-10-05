# run with `pytest roles/gitops/rendering-apps-files/library/test_render_apps.py -vv -s`

import pytest, yaml, json, shutil
from pathlib import Path
from types import SimpleNamespace
from render_apps import (
    ansible_dict2items,
    ansible_to_nice_yaml,
    deep_merge,
    deep_merge_with_warnings,
    merge_list,
    normalize_ca_list,
    load_yaml_unique,
    render_app,
    render_all,
    render_dsc_json,
    RenderContext,
)

REAL_ROLE = Path(__file__).resolve().parent.parent

@pytest.mark.parametrize("data, kwargs, expected", [
    ({"foo": "bar", "hello": "world"}, {}, [{"key": "foo", "value": "bar"}, {"key": "hello", "value": "world"}]),
    ({"foo": "bar"}, {"key_name": "name", "value_name": "content"}, [{"name": "foo", "content": "bar"}]),
    ("not a dict", {}, []),
    ([], {}, []),
    (None, {}, []),
])
def test_dict2items(data, kwargs, expected):
    assert ansible_dict2items(data, **kwargs) == expected

@pytest.mark.parametrize("content", [
    "foo: first\nfoo: second\n",
    "outer:\n  foo: first\n  foo: second\n",
    "policy.csv: |\n  first\npolicy.csv: |\n  second\n",
])
def test_load_yaml_unique_rejects_duplicate_keys(content):
    with pytest.raises(yaml.constructor.ConstructorError):
        load_yaml_unique(content)

def test_load_yaml_unique_loads_normal_yaml():
    assert load_yaml_unique("foo: bar\nanswer: 42\n") == {"foo": "bar", "answer": 42}

@pytest.mark.parametrize("data, expected", [
    ({"script": "line one\nline two\n"}, "script: |"),
    ({"config": {"certificate": "-----BEGIN CERTIFICATE-----\nABC\n-----END CERTIFICATE-----"}}, "certificate: |"),
])
def test_ansible_to_nice_yaml_uses_literal_style(data, expected):
    assert expected in ansible_to_nice_yaml(data)

def test_ansible_to_nice_yaml_keeps_single_line_plain():
    assert "name: hello" in ansible_to_nice_yaml({"name": "hello"})

@pytest.mark.parametrize("raw, expected", [
    (None, []),
    ("", []),
    ([], []),
    ("  CERTIFICATE  ", ["CERTIFICATE"]),
    (["  CERT-A  ", "", "CERT-B\n", None], ["CERT-A", "CERT-B"]),
])
def test_normalize_ca_list(raw, expected):
    assert normalize_ca_list(raw) == expected

@pytest.mark.parametrize("base, override, expected", [
    ({"foo": {"one": 1, "two": 2}}, {"foo": {"two": 20, "three": 3}}, {"foo": {"one": 1, "two": 20, "three": 3}}),
    ({"foo": "old"}, {"foo": "new"}, {"foo": "new"}),
    ({"items": [1, 2]}, {"items": [3]}, {"items": [3]}),
    ({"foo": "bar"}, {"new": "value"}, {"foo": "bar", "new": "value"}),
    ({"foo": {"bar": 1}}, {"foo": "replacement"}, {"foo": "replacement"}),
])
def test_deep_merge(base, override, expected):
    assert deep_merge(base, override) == expected

def test_deep_merge_does_not_modify_base():
    base = {"foo": {"bar": 1}}
    deep_merge(base, {"foo": {"bar": 2}})
    assert base == {"foo": {"bar": 1}}

class TestMergeList:

    @pytest.mark.parametrize("base, override, expected, warnings", [
        ([{"name": "FOO", "value": "old"}, {"name": "KEEP", "value": "yes"}],
         [{"name": "FOO", "value": "new"}],
         [{"name": "FOO", "value": "new"}, {"name": "KEEP", "value": "yes"}],
         ["dsc override at 'env.[name=FOO].value' replaces role default"]),
        ([{"name": "FOO", "value": "bar"}], [{"name": "NEW", "value": "value"}],
         [{"name": "FOO", "value": "bar"}, {"name": "NEW", "value": "value"}],
         ["List at 'env' gained new entry 'name=NEW' from dsc override"]),
        ([{"name": "FOO", "value": "old", "metadata": {"one": 1, "two": 2}}],
         [{"name": "FOO", "metadata": {"two": 20, "three": 3}}],
         [{"name": "FOO", "value": "old", "metadata": {"one": 1, "two": 20, "three": 3}}],
         ["dsc override at 'env.[name=FOO].metadata.two' replaces role default"]),
    ])
    def test_env_merge(self, base, override, expected, warnings):
        ctx = make_render_context(Path("."), Path("."))

        result = merge_list(ctx, base, override, ["env"])

        assert result == expected
        assert ctx.warnings == warnings

    def test_unknown_list_field_is_replaced(self):
        ctx = make_render_context(Path("."), Path("."))

        result = merge_list(ctx, ["one", "two"], ["three"], ["someUnknownField"])

        assert result == ["three"]
        assert ctx.warnings == [
            "List at 'someUnknownField' fully replaced by dsc override (no known merge key for this field)"
        ]

    def test_empty_base_unknown_list_does_not_warn(self):
        ctx = make_render_context(Path("."), Path("."))

        result = merge_list(ctx, [], ["new"], ["someUnknownField"])

        assert result == ["new"]
        assert ctx.warnings == []

    def test_env_from_is_merged_by_reference_name(self):
        ctx = make_render_context(Path("."), Path("."))
        base = [
            {"secretRef": {"name": "base-secret"}},
            {"configMapRef": {"name": "base-config"}},
        ]
        override = [{"secretRef": {"name": "base-secret", "optional": True}}]

        result = merge_list(ctx, base, override, ["envFrom"])

        assert result == [
            {"secretRef": {"name": "base-secret", "optional": True}},
            {"configMapRef": {"name": "base-config"}},
        ]
        assert ctx.errors == []

    def test_env_from_without_reference_is_reported_without_key_error(self):
        ctx = make_render_context(Path("."), Path("."))

        result = merge_list(
            ctx,
            [{"secretRef": {"name": "base-secret"}}],
            [{"value": "not-a-reference"}],
            ["envFrom"],
        )

        assert result == [{"value": "not-a-reference"}]
        assert any("usable merge key" in error for error in ctx.errors)

    def test_ports_use_container_port_or_service_port(self):
        ctx = make_render_context(Path("."), Path("."))

        container_ports = merge_list(
            ctx,
            [{"name": "http", "containerPort": 8080}],
            [{"name": "http", "containerPort": 8080, "protocol": "TCP"}],
            ["ports"],
        )
        service_ports = merge_list(
            ctx,
            [{"name": "http", "port": 80}],
            [{"name": "http", "port": 80, "targetPort": 8080}],
            ["ports"],
        )

        assert container_ports == [{"name": "http", "containerPort": 8080, "protocol": "TCP"}]
        assert service_ports == [{"name": "http", "port": 80, "targetPort": 8080}]
        assert ctx.errors == []

    def test_extra_env_from_uses_env_from_rules(self):
        ctx = make_render_context(Path("."), Path("."))
        base = {"cpn-ansible-job": {"job": {"extraEnvFrom": [{"secretRef": {"name": "base"}}]}}}
        override = {"cpn-ansible-job": {"job": {"extraEnvFrom": [{"secretRef": {"name": "base", "optional": True}}]}}}

        result = deep_merge_with_warnings(ctx, base, override, [])

        assert result["cpn-ansible-job"]["job"]["extraEnvFrom"] == [
            {"secretRef": {"name": "base", "optional": True}}
        ]
        assert ctx.errors == []

class TestDeepMergeWithWarnings:

    @pytest.mark.parametrize("base, override, expected, warnings", [
        ({"replicas": 2}, {"replicas": 3}, {"replicas": 3},
         ["dsc override at 'replicas' replaces role default"]),
        ({"controller": {"replicas": 2}}, {"controller": {"replicas": 3}},
         {"controller": {"replicas": 3}},
         ["dsc override at 'controller.replicas' replaces role default"]),
        ({"foo": "bar"}, {"new": "value"}, {"foo": "bar", "new": "value"}, []),
        ({"foo": "bar"}, {"foo": "bar"}, {"foo": "bar"}, []),
        ({"env": [{"name": "FOO", "value": "old"}]}, {"env": [{"name": "FOO", "value": "new"}]},
         {"env": [{"name": "FOO", "value": "new"}]},
         ["dsc override at 'env.[name=FOO].value' replaces role default"]),
        ({"configs": {"cm": {"timeout.reconciliation": "180s", "status.processors": "10"}}},
         {"configs": {"cm": {"timeout.reconciliation": "60s"}}},
         {"configs": {"cm": {"timeout.reconciliation": "60s", "status.processors": "10"}}},
         ["dsc override at 'configs.cm.timeout.reconciliation' replaces role default"]),
    ])
    def test_merge_and_warnings(self, base, override, expected, warnings):
        ctx = make_render_context(Path("."), Path("."))

        result = deep_merge_with_warnings(ctx, base, override, [])

        assert result == expected
        assert ctx.warnings == warnings


def create_test_role(tmp_path, app_name="testapp"):
    "Create a minimal role structure that render_app() can consume."
    role_path = tmp_path / "role"
    gitops_path = tmp_path / "gitops"

    app_path = role_path / "templates" / app_name
    templates_path = app_path / "templates"
    files_path = app_path / "files"
    values_path = app_path / "values"

    templates_path.mkdir(parents=True)
    files_path.mkdir(parents=True)
    values_path.mkdir(parents=True)

    (app_path / "Chart.yaml.j2").write_text(
        """
apiVersion: v2
name: {{ app_name }}
version: 1.0.0
""".strip()
        + "\n"
    )

    (templates_path / "deployment.yaml.j2").write_text(
        """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ app_name }}
  namespace: {{ env }}
""".strip()
        + "\n"
    )

    (files_path / "static.txt").write_text("static content\n")

    (values_path / "100-base.j2").write_text(
        """
testapp:
  replicas: 2
  image:
    repository: example/app
    tag: base
""".strip()
        + "\n"
    )

    return role_path, gitops_path

def create_argocd_role_vars(role_path):
    vars_path = role_path / "vars"
    vars_path.mkdir(parents=True, exist_ok=True)

    argocd_path = role_path / "templates" / "argocd"
    argocd_path.mkdir(parents=True, exist_ok=True)

    values_path = argocd_path / "values"
    values_path.mkdir(parents=True, exist_ok=True)

    (argocd_path / "Chart.yaml.j2").write_text(
        """
apiVersion: v2
name: argocd
version: 1.0.0
""".strip()
        + "\n"
    )

    (values_path / "100-base.j2").write_text(
        """
argocd:
  configs:
    rbac:
      policy.default: role:base
    cm:
      timeout.reconciliation: 180s
      oidc.config: |
        issuer: https://example.test
        requestedScopes:
          - openid
""".strip()
        + "\n"
    )

    (vars_path / "main.yaml").write_text(
        """
values:
  argocd:
    rootCA_block: |
      rootCA: |
        -----BEGIN CERTIFICATE-----
        ROLE-CA
        -----END CERTIFICATE-----
""".strip()
        + "\n"
    )

def make_dsc(app_name="testapp", enabled=True, values=None):
    """Create the minimum DSC structure required by render_app()."""
    return {
        "global": {"gitOps": {"repo": {"path": "clusters"}}},
        app_name: {"installEnabled": enabled, "values": values or {}},
    }

def make_task(app_name="testapp", env_name="dev"):
    return (env_name, {"argocd_app": app_name})

def make_render_context(role_path, gitops_path, dsc=None, app_name="testapp", env_name="dev", jinja_context=None, role_vars=None):
    "Build a RenderContext directly, for tests of the merge helpers."

    return RenderContext(
        app_name=app_name,
        env_name=env_name,
        dsc=dsc or make_dsc(app_name),
        role_path=role_path,
        gitops_local_repo=gitops_path,
        source_path=role_path / "templates" / app_name,
        destination_path=gitops_path / "clusters" / "envs" / env_name / "apps" / app_name,
        role_vars=role_vars or {},
        jinja_context=jinja_context or {"app_name": app_name},
    )

def run_render_app(role_path, gitops_path, dsc=None, app_name="testapp", env_name="dev", **context):
    "Call render_app() with app_name injected into the ambient context, as Ansible does."
    dsc = dsc or make_dsc(app_name)
    env_tuple = make_task(app_name, env_name)
    full_context = {"app_name": app_name, **context}
    return render_app(env_tuple, dsc, role_path, gitops_path, full_context)

def app_dest(gitops_path, app_name="testapp", env_name="dev"):
    "Where render_app() writes an app's output, under the repo path from make_dsc()."
    return gitops_path / "clusters" / "envs" / env_name / "apps" / app_name

def read_values(gitops_path, app_name="testapp", env_name="dev"):
    return yaml.safe_load((app_dest(gitops_path, app_name, env_name) / "values.yaml").read_text())

def read_gitlab(gitops_path):
    return yaml.safe_load((app_dest(gitops_path, "gitlab") / "templates" / "gitlab.yaml").read_text())

def make_argocd_role(tmp_path):
    "Role and dsc with the argocd rootCA_block vars; returns role_path, gitops_path, dsc."
    role_path, gitops_path = create_test_role(tmp_path)
    create_argocd_role_vars(role_path)
    dsc = make_dsc(app_name="argocd")
    dsc["exposedCA"] = {"type": "secret"}
    return role_path, gitops_path, dsc

def run_argocd(role_path, gitops_path, dsc):
    return run_render_app(
        role_path, gitops_path, dsc, app_name="argocd",
        vaultinfra_kv_name="vault", dsc_name="conf-dso",
    )

def create_vault_role(tmp_path):
    "Temporary role using the real Vault values template, with a generic Chart.yaml and manifest."
    role_path, gitops_path = create_test_role(tmp_path, app_name="vault")
    vault_values = role_path / "templates" / "vault" / "values"
    (vault_values / "100-base.j2").unlink()
    shutil.copy(REAL_ROLE / "templates" / "vault" / "values" / "00-main.j2", vault_values / "00-main.j2")
    return role_path, gitops_path

# Fixtures hide the role/repo plumbing from the integration tests.
@pytest.fixture
def app(tmp_path):
    role_path, gitops_path = create_test_role(tmp_path)
    return SimpleNamespace(role=role_path, gitops=gitops_path)

@pytest.fixture
def render(app):
    def _render(dsc=None, app_name="testapp", **context):
        return run_render_app(app.role, app.gitops, dsc, app_name=app_name, **context)
    return _render

@pytest.fixture
def argocd(tmp_path):
    role_path, gitops_path, dsc = make_argocd_role(tmp_path)
    return SimpleNamespace(role=role_path, gitops=gitops_path, dsc=dsc)

@pytest.fixture
def render_argocd(argocd):
    def _render(dsc=None):
        return run_argocd(argocd.role, argocd.gitops, dsc or argocd.dsc)
    return _render

@pytest.fixture
def gitlab(tmp_path):
    role_path, gitops_path = create_test_role(tmp_path, app_name="gitlab")
    dsc = make_dsc(app_name="gitlab")
    dsc["gitlab"].update(namespace="gitlab-ns", chartVersion="8.0.0")
    return SimpleNamespace(role=role_path, gitops=gitops_path, dsc=dsc)

@pytest.fixture
def render_gitlab(gitlab):
    def _render():
        return run_render_app(gitlab.role, gitlab.gitops, gitlab.dsc, app_name="gitlab")
    return _render

@pytest.fixture
def dsc_json(tmp_path):
    return SimpleNamespace(
        gitops=tmp_path / "gitops",
        dsc={"global": {"gitOps": {"namespacePrefix": "team-a-"}}},
    )

class TestRenderAppFiles:

    def test_files_are_created(self, app, render):
        has_error, changed, message = render()

        assert has_error is False
        assert changed is True

        dest = app_dest(app.gitops)
        assert (dest / "Chart.yaml").read_text() == "apiVersion: v2\nname: testapp\nversion: 1.0.0\n"
        assert (dest / "templates" / "deployment.yaml").read_text() == (
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: testapp\n  namespace: dev\n"
        )
        assert (dest / "static.txt").read_text() == "static content\n"
        assert read_values(app.gitops) == {
            "testapp": {"replicas": 2, "image": {"repository": "example/app", "tag": "base"}},
        }

    def test_static_file_change_marks_app_as_changed(self, app, render):
        render()
        has_error, changed, message = render()

        assert has_error is False
        assert changed is False

        source = app.role / "templates" / "testapp" / "files" / "static.txt"
        source.write_text("updated static content\n")

        has_error, changed, message = render()

        assert has_error is False
        assert changed is True
        assert (app_dest(app.gitops) / "static.txt").read_text() == "updated static content\n"

    def test_output_directory_is_created_automatically(self, app, render):
        assert not app.gitops.exists()  # don't create it ourselves

        has_error, changed, message = render()

        assert has_error is False
        assert changed is True
        assert app_dest(app.gitops).is_dir()

class TestRenderAppDisabled:

    def test_disabled_app_is_not_rendered(self, app, render):
        has_error, changed, message = render(make_dsc(enabled=False))

        assert has_error is False
        assert changed is False
        assert "Skipped testapp in dev" in message
        assert not app_dest(app.gitops).exists()

class TestRenderAppValues:

    def test_dsc_values_override_base_values(self, app, render):
        dsc = make_dsc(values={"replicas": 5, "image": {"tag": "override"}})

        has_error, changed, message = render(dsc)

        assert has_error is False
        assert changed is True
        assert read_values(app.gitops) == {
            "testapp": {"replicas": 5, "image": {"repository": "example/app", "tag": "override"}},
        }
        assert "WARNING" in message
        assert "replicas" in message
        assert "image.tag" in message

    def test_wrapper_values_include_cluster_and_ansible_job(self, app, render):
        values_dir = app.role / "templates" / "testapp" / "values"
        (values_dir / "200-ansible-job.j2").write_text(
            """
    cpn-ansible-job:
    job:
        extraEnv:
        - name: TEST_VAR
          value: test
    """.strip()
            + "\n"
        )

        dsc = make_dsc(values={"replicas": 5})
        dsc["testapp"]["cnpg"] = {"values": {"instances": 3, "storage": {"size": "10Gi"}}}
        dsc["cpnAnsibleJob"] = {
            "values": {"job": {"extraVolumeMounts": [{"name": "creds", "mountPath": "/etc/creds"}]}}
        }
        dsc["testapp"]["repoSocle"] = {"url": "https://example.com/socle.git", "revision": "main"}

        has_error, changed, message = render(dsc)

        assert not has_error, message

        values = read_values(app.gitops)
        assert values["testapp"]["replicas"] == 5
        assert values["cluster"] == {"instances": 3, "storage": {"size": "10Gi"}}
        assert values["cpn-ansible-job"]["job"]["extraEnv"] == [{"name": "TEST_VAR", "value": "test"}]
        assert values["cpn-ansible-job"]["job"]["extraVolumeMounts"] == [
            {"name": "creds", "mountPath": "/etc/creds"}
        ]

    def test_cpn_ansible_job_without_template_is_reported(self, app, render):
        dsc = make_dsc()
        dsc["cpnAnsibleJob"] = {"values": {}}
        dsc["testapp"]["repoSocle"] = {"url": "https://example.com/socle.git", "revision": "main"}

        has_error, changed, message = render(dsc)

        assert has_error is True
        assert "cpnAnsibleJob" in message
        assert "200-ansible-job.j2" in message

    def test_stale_generated_template_is_deleted(self, app, render):
        templates_dest = app_dest(app.gitops) / "templates"

        has_error, changed, message = render()
        assert has_error is False
        assert changed is True

        generated_file = templates_dest / "deployment.yaml"
        assert generated_file.exists()

        (app.role / "templates" / "testapp" / "templates" / "deployment.yaml.j2").unlink()

        has_error, changed, message = render()
        assert has_error is False
        assert changed is True
        assert not generated_file.exists()

    def test_stale_template_is_deleted(self, app, render):
        templates_dest = app_dest(app.gitops) / "templates"

        has_error, changed, message = render()
        assert has_error is False
        assert changed is True

        deployment = templates_dest / "deployment.yaml"
        assert deployment.exists()

        stale = templates_dest / "old.yaml"
        stale.write_text("this should disappear\n")

        has_error, changed, message = render()
        assert has_error is False
        assert changed is True
        assert deployment.exists()
        assert not stale.exists()

    def test_whitelisted_gitlab_yaml_is_preserved(self, app, render):
        templates_dest = app_dest(app.gitops) / "templates"
        templates_dest.mkdir(parents=True, exist_ok=True)
        gitlab_file = templates_dest / "gitlab.yaml"
        gitlab_file.write_text("managed externally\n")

        has_error, changed, message = render()

        assert has_error is False
        assert changed is True
        assert gitlab_file.exists()
        assert gitlab_file.read_text() == "managed externally\n"

    def test_empty_template_deletes_existing_file(self, app, render):
        templates_dest = app_dest(app.gitops) / "templates"

        render()
        generated_file = templates_dest / "deployment.yaml"
        assert generated_file.exists()

        (app.role / "templates" / "testapp" / "templates" / "deployment.yaml.j2").write_text("")
        render()

        assert not generated_file.exists()

    def test_argocd_dsc_values_override_role_defaults(self, argocd, render_argocd):
        argocd.dsc["argocd"]["values"] = {
            "configs": {
                "rbac": {"policy.default": "role:custom"},
                "cm": {"timeout.reconciliation": "60s"},
            }
        }

        has_error, changed, message = render_argocd()

        assert not has_error, message
        assert changed is True

        values = read_values(argocd.gitops, "argocd")
        assert values["argocd"]["configs"]["rbac"]["policy.default"] == "role:custom"
        assert values["argocd"]["configs"]["cm"]["timeout.reconciliation"] == "60s"

    def test_argocd_oidc_root_ca_is_combined(self, argocd, render_argocd):
        argocd.dsc["argocd"]["values"] = {
            "oidcRootCA": "-----BEGIN CERTIFICATE-----\nUSER-CA\n-----END CERTIFICATE-----"
        }

        has_error, changed, message = render_argocd()

        assert not has_error, message
        assert changed is True

        values = read_values(argocd.gitops, "argocd")
        assert "oidcRootCA" not in values["argocd"]

        oidc_config = yaml.safe_load(values["argocd"]["configs"]["cm"]["oidc.config"])
        assert oidc_config["requestedScopes"] == ["openid"]
        assert oidc_config["issuer"] == "https://example.test"
        assert "ROLE-CA" in oidc_config["rootCA"]
        assert "USER-CA" in oidc_config["rootCA"]

    def test_argocd_root_ca_placeholder_keeps_block_scalar(self, argocd, render_argocd):
        (argocd.role / "vars" / "main.yaml").write_text(
            """
values:
  argocd:
    rootCA_block: |
      rootCA: |
        <path:{{ vaultinfra_kv_name }}/data/env/{{ dsc_name }}/apps/global/values#exposedCa | indent 2>
""".strip()
            + "\n"
        )

        has_error, changed, message = render_argocd()

        assert not has_error, message
        raw = (app_dest(argocd.gitops, "argocd") / "values.yaml").read_text()
        assert "rootCA: |-" in raw
        assert "<path:vault/data/env/conf-dso/apps/global/values#exposedCa | indent 2>" in raw

    def test_argocd_root_ca_is_absent_when_exposed_ca_is_none(self, argocd, render_argocd):
        argocd.dsc["exposedCA"] = {"type": "none"}

        has_error, changed, message = render_argocd()

        assert not has_error, message
        values = read_values(argocd.gitops, "argocd")
        oidc_config = yaml.safe_load(values["argocd"]["configs"]["cm"]["oidc.config"])
        assert "rootCA" not in oidc_config

    def test_argocd_oidc_root_ca_is_used_when_exposed_ca_is_none(self, argocd, render_argocd):
        argocd.dsc["exposedCA"] = {"type": "none"}
        argocd.dsc["argocd"]["values"] = {
            "oidcRootCA": "-----BEGIN CERTIFICATE-----\nUSER-CA\n-----END CERTIFICATE-----"
        }

        has_error, changed, message = render_argocd()

        assert not has_error, message
        values = read_values(argocd.gitops, "argocd")
        assert "oidcRootCA" not in values["argocd"]
        oidc_config = yaml.safe_load(values["argocd"]["configs"]["cm"]["oidc.config"])
        assert "USER-CA" in oidc_config["rootCA"]
        assert "ROLE-CA" not in oidc_config["rootCA"]

    def test_argocd_metrics_additional_labels(self, argocd, render_argocd):
        argocd.dsc["global"]["metrics"] = {"additionalLabels": {"team": "platform"}}

        has_error, changed, message = render_argocd()

        assert not has_error, message
        assert changed is True

        values = read_values(argocd.gitops, "argocd")
        application_labels = values["argocd"]["controller"]["metrics"]["applicationLabels"]
        assert application_labels["enabled"] is True
        assert application_labels["labels"] == {"team": "platform"}

        assert "labels" not in values["argocd"].get("controller", {})
        service_monitor = values["argocd"].get("controller", {}).get("metrics", {}).get("serviceMonitor", {})
        assert "labels" not in service_monitor

    def test_argocd_metrics_additional_labels_absent_by_default(self, argocd, render_argocd):
        has_error, changed, message = render_argocd()

        assert not has_error, message
        assert changed is True

        values = read_values(argocd.gitops, "argocd")
        assert "applicationLabels" not in values["argocd"].get("controller", {}).get("metrics", {})


class TestVaultValues:
    def test_raft_config_disables_mlock(self, tmp_path):
        role_path, gitops_path = create_vault_role(tmp_path)
        dsc = {
            "global": {
                "gitOps": {"repo": {"path": "clusters"}},
                "metrics": {"enabled": True, "additionalLabels": {"team": "platform"}},
            },
            "vault": {"installEnabled": True, "pvcSize": "10Gi"},
        }

        has_error, changed, message = run_render_app(
            role_path, gitops_path, dsc, app_name="vault",
            dsc_name="conf-dso", vaultinfra_kv_name="vault",
        )

        assert not has_error, message
        values = read_values(gitops_path, "vault")
        raft_config = values["vault"]["server"]["ha"]["raft"]["config"]
        assert "disable_mlock = true" in raft_config


class TestGitLabDefinition:
    "gitlab.yaml: the GitLab operator CR, distinct from the dsc.gitlabOperator chart values."

    def test_gitlab_cr_is_generated(self, gitlab, render_gitlab):
        gitlab.dsc["gitlab"]["values"] = {"global": {"edition": "ce"}}

        has_error, changed, message = render_gitlab()

        assert not has_error, message
        assert changed is True

        definition = read_gitlab(gitlab.gitops)
        assert definition["apiVersion"] == "apps.gitlab.com/v1beta1"
        assert definition["kind"] == "GitLab"
        assert definition["metadata"] == {
            "name": "gitlab", "namespace": "gitlab-ns",
            "annotations": {"argocd.argoproj.io/sync-wave": "5"},
        }
        assert definition["spec"]["chart"] == {"version": "8.0.0", "values": {"global": {"edition": "ce"}}}

    def test_gitlab_cr_merges_role_defaults_with_dsc_values(self, gitlab, render_gitlab):
        values_dir = gitlab.role / "gitlab" / "values"
        values_dir.mkdir(parents=True)
        (values_dir / "100-base.j2").write_text("global:\n  edition: ee\ncertmanager:\n  install: true\n")
        gitlab.dsc["gitlab"]["values"] = {"global": {"edition": "ce"}}

        has_error, changed, message = render_gitlab()

        assert not has_error, message
        assert read_gitlab(gitlab.gitops)["spec"]["chart"]["values"] == {
            "global": {"edition": "ce"}, "certmanager": {"install": True},
        }

    def test_gitlab_cr_survives_cleanup_and_is_not_rewritten(self, gitlab, render_gitlab):
        # No gitlab.yaml.j2 source exists, so the whitelist keeps the CR across stale cleanup.
        render_gitlab()
        gitlab_yaml = app_dest(gitlab.gitops, "gitlab") / "templates" / "gitlab.yaml"
        assert gitlab_yaml.exists()

        has_error, changed, message = render_gitlab()

        assert not has_error, message
        assert changed is False
        assert gitlab_yaml.exists()

    def test_missing_namespace_is_reported_as_error(self, gitlab, render_gitlab):
        del gitlab.dsc["gitlab"]["namespace"]

        has_error, changed, message = render_gitlab()

        assert has_error is True
        assert changed is True
        assert "GitLab definition error" in message

class TestRenderAppErrors:

    def test_invalid_template_is_reported_as_error(self, tmp_path):
        role_path, gitops_path = create_test_role(tmp_path)
        template = role_path / "templates" / "testapp" / "templates" / "deployment.yaml.j2"
        template.write_text("{{ this_is_not_valid_jinja ")

        has_error, changed, message = run_render_app(role_path, gitops_path)

        assert has_error is True
        assert changed is True
        assert "ERROR" in message
        assert "deployment.yaml.j2" in message

    def test_duplicate_keys_in_values_are_reported(self, tmp_path):
        role_path, gitops_path = create_test_role(tmp_path)
        values_file = role_path / "templates" / "testapp" / "values" / "100-base.j2"
        values_file.write_text("foo: first\nfoo: second\n")

        has_error, changed, message = run_render_app(role_path, gitops_path)

        assert has_error is True
        assert changed is True
        assert "Base value 100-base.j2 error" in message

class TestRenderAllDscJson:
    """The dsc.json is generated for a normal install, not for post-conf jobs."""

    def _dsc(self):
        return {"global": {"gitOps": {
            "repo": {"path": "clusters"},
            "namespacePrefix": "team-a-",
        }}}

    def test_dsc_json_is_rendered_without_post_conf_job(self, tmp_path):
        envs = [{"name": "dev", "apps": []}]

        changed, results, has_errors = render_all(
            self._dsc(), envs, tmp_path, tmp_path, {"dsc_name": "conf-dso"},
        )

        assert has_errors is False
        assert changed is True
        assert (tmp_path / "clusters" / "envs" / "dev" / "conf-dso.json").exists()

    def test_dsc_json_is_not_rendered_in_post_conf_job(self, tmp_path):
        envs = [{"name": "dev", "apps": []}]

        changed, results, has_errors = render_all(
            self._dsc(), envs, tmp_path, tmp_path,
            {"post_conf_job": True, "dsc_name": "conf-dso"},
        )

        assert has_errors is False
        assert changed is False
        assert not (tmp_path / "clusters" / "envs" / "dev" / "conf-dso.json").exists()


class TestDscJson:
    "<dsc_name>.json: a per-env summary of app topology."

    def test_dsc_json_is_rendered(self, dsc_json):
        dsc_json.dsc["global"]["gitOps"].update(
            repo={"path": "clusters", "revision": "main"}, envName="prod-cluster")
        dsc_json.dsc["testapp"] = {"installEnabled": True}
        env = {"name": "dev", "apps": [
            {"argocd_app": "testapp", "nameSpace": "testapp-ns", "clusterName": "dev-cluster",
             "customNamespacePrefix": "x-", "syncWave": 3},
        ]}

        changed, message = render_dsc_json(dsc_json.dsc, 'conf-dso', env, dsc_json.gitops)

        assert changed is True
        data = json.loads((dsc_json.gitops / "clusters" / "envs" / "dev" / "conf-dso.json").read_text())
        assert data == {
            "env": "dev",
            "provider": "self-hosted",
            "region": "fr-par",
            "namespacePrefix": "team-a-",
            "destination": {"clusterName": "prod-cluster"},
            "targetRevision": "main",
            "apps": [{
                "app": "testapp", "enabled": True, "clusterName": "dev-cluster",
                "namespace": "testapp-ns", "customNamespacePrefix": "x-", "syncWave": 3,
            }],
        }

    def test_dsc_json_apps_use_defaults(self, dsc_json):
        env = {"name": "dev", "apps": [{"argocd_app": "testapp", "nameSpace": "testapp-ns"}]}

        render_dsc_json(dsc_json.dsc, 'conf-dso', env, dsc_json.gitops)

        data = json.loads((dsc_json.gitops / "envs" / "dev" / "conf-dso.json").read_text())
        assert data["destination"] == {"clusterName": ""}
        assert data["targetRevision"] == "main"
        assert data["apps"][0] == {
            "app": "testapp", "enabled": False, "clusterName": "",
            "namespace": "testapp-ns", "customNamespacePrefix": "", "syncWave": 10,
        }

    def test_observability_enabled_reads_from_observatorium(self, dsc_json):
        dsc_json.dsc["observatorium"] = {"installEnabled": True}
        env = {"name": "dev", "apps": [{"argocd_app": "observability", "nameSpace": "obs-ns"}]}

        render_dsc_json(dsc_json.dsc, 'conf-dso', env, dsc_json.gitops)

        data = json.loads((dsc_json.gitops / "envs" / "dev" / "conf-dso.json").read_text())
        assert data["apps"][0]["enabled"] is True

    def test_env_with_no_apps_key_renders_empty_apps(self, dsc_json):
        env = {"name": "dev"} # no "apps" key at all

        render_dsc_json(dsc_json.dsc, 'conf-dso', env, dsc_json.gitops)

        data = json.loads((dsc_json.gitops / "envs" / "dev" / "conf-dso.json").read_text())
        assert data["apps"] == []

    def test_second_identical_render_is_not_rewritten(self, dsc_json):
        env = {"name": "dev", "apps": []}

        render_dsc_json(dsc_json.dsc, 'conf-dso', env, dsc_json.gitops)
        changed, message = render_dsc_json(dsc_json.dsc, 'conf-dso', env, dsc_json.gitops)

        assert changed is False

    def test_missing_namespace_prefix_raises(self, dsc_json):
        del dsc_json.dsc["global"]["gitOps"]["namespacePrefix"]
        env = {"name": "dev", "apps": []}

        with pytest.raises(KeyError):
            render_dsc_json(dsc_json.dsc, 'conf-dso', env, dsc_json.gitops)
