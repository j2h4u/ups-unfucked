"""Safety-first startup discovery that defers event projection."""

from dataclasses import dataclass

from src.application.assessment_worker import AssessmentWorker, CloseRequest
from src.application.ports import StartupRecoveryEventStorePort
from src.application.storage_values import ProcessingRef, RecoveredCapture


@dataclass(frozen=True, slots=True)
class StartupRecovery:
    recovered_capture: RecoveredCapture | None
    pending_processing: tuple[ProcessingRef, ...]


def recover_startup_metadata(store: StartupRecoveryEventStorePort) -> StartupRecovery:
    """Recover bounded registry/capture metadata without opening pending events."""
    recovered_capture = store.recover_startup()
    registry = store.work_registry()
    return StartupRecovery(recovered_capture, registry.pending_processing)


def defer_processing_after_first_publication(
    recovery: StartupRecovery,
    worker: AssessmentWorker,
) -> int:
    """Activate assessment and enqueue only bounded registry references."""
    worker.after_first_safety_publication()
    accepted = 0
    for processing in recovery.pending_processing:
        if worker.defer(CloseRequest(processing)):
            accepted += 1
    return accepted
