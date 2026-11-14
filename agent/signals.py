"""The one data shape every detector emits."""

import time
from dataclasses import dataclass, field

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


@dataclass
class Signal:
    source: str        # watch | alert | logs | probe | sweep
    namespace: str
    workload: str      # deployment/statefulset name, or a node / pvc name for those kinds
    symptom: str       # CrashLoopBackOff, OOMKilled, ProbeFailed, HighErrorRate, ...
    summary: str
    severity: str = "warning"
    evidence: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)
    stateful: bool = True  # True: re-checkable from current cluster state (watch/sweep)

    @property
    def key(self) -> str:
        """Incidents are grouped per workload; symptoms accumulate as evidence."""
        return f"{self.namespace}/{self.workload}"

    def to_dict(self) -> dict:
        return {
            "source": self.source, "namespace": self.namespace, "workload": self.workload,
            "symptom": self.symptom, "summary": self.summary, "severity": self.severity,
            "evidence": self.evidence, "ts": self.ts,
        }
