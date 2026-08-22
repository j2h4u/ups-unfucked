# Vulture whitelist — only static-analysis false positives with a specific
# framework/dataclass/protocol/serialization reason. Genuine dead code stays
# visible to the hard gate. Reviewed 2026-08-16.

# The assessment codec walks dataclass fields by calling dataclasses.fields()
# and getattr(), so vulture cannot see these persisted JSON fields as reads.
battery_voltage_raw  # FALSE POSITIVE: PhysicalObservation JSONL field (domain/values.py)
mean  # FALSE POSITIVE: NumericSummary JSONL field (domain/values.py)
population_stddev  # FALSE POSITIVE: NumericSummary JSONL field (domain/values.py)
voltage_summary  # FALSE POSITIVE: EvidenceAssessment JSONL field (domain/values.py)
load_summary  # FALSE POSITIVE: EvidenceAssessment JSONL field (domain/values.py)
evaluation_origin_monotonic_ns  # FALSE POSITIVE: ForwardComparison JSONL field (domain/values.py)
start_residual_v  # FALSE POSITIVE: ForwardComparison JSONL field (domain/values.py)
mean_residual_v  # FALSE POSITIVE: ForwardComparison JSONL field (domain/values.py)
rmse_v  # FALSE POSITIVE: ForwardComparison JSONL field (domain/values.py)
observed_slope_v_per_s  # FALSE POSITIVE: ForwardComparison JSONL field (domain/values.py)
predicted_slope_v_per_s  # FALSE POSITIVE: ForwardComparison JSONL field (domain/values.py)
step_id  # FALSE POSITIVE: LoadStepEstimate JSONL field (domain/values.py)
pre_sequences  # FALSE POSITIVE: LoadStepEstimate JSONL field (domain/values.py)
post_sequences  # FALSE POSITIVE: LoadStepEstimate JSONL field (domain/values.py)
pre_slope_v_per_s  # FALSE POSITIVE: LoadStepEstimate JSONL field (domain/values.py)
early_post_slope_v_per_s  # FALSE POSITIVE: LoadStepEstimate JSONL field (domain/values.py)
late_post_slope_v_per_s  # FALSE POSITIVE: LoadStepEstimate JSONL field (domain/values.py)
early_delta_voltage_at_transition_v  # FALSE POSITIVE: LoadStepEstimate JSONL field (domain/values.py)
settled_delta_voltage_at_transition_v  # FALSE POSITIVE: LoadStepEstimate JSONL field (domain/values.py)
voltage_quantum_v  # FALSE POSITIVE: LoadStepEstimate JSONL field (domain/values.py)
k_transition_v_per_pp  # FALSE POSITIVE: LoadStepEstimate JSONL field (domain/values.py)
record_history  # FALSE POSITIVE: LearningDecision JSONL field (domain/values.py)
scientific_fingerprint_before  # FALSE POSITIVE: ModelCommitReceipt JSONL field (domain/values.py)
reference_reparameterization  # FALSE POSITIVE: ModelCommitReceipt JSONL field (domain/values.py)

# Health/publication projections use dataclasses.asdict(), so these fields are
# read reflectively by the JSON health publisher rather than by name.
maintenance_queued  # FALSE POSITIVE: CaptureQueueHealth health JSON field (application/storage_values.py)
max_busy_time_s  # FALSE POSITIVE: CaptureQueueHealth health JSON field (application/storage_values.py)
oldest_queue_age_s  # FALSE POSITIVE: CaptureQueueHealth health JSON field (application/storage_values.py)
durability_lag_s  # FALSE POSITIVE: StorageHealth health JSON field (application/storage_values.py)
rebuild_generation  # FALSE POSITIVE: StorageHealth health JSON field (application/storage_values.py)
rebuild_files_remaining  # FALSE POSITIVE: StorageHealth health JSON field (application/storage_values.py)
rebuild_last_progress_utc  # FALSE POSITIVE: StorageHealth health JSON field (application/storage_values.py)
event_count  # FALSE POSITIVE: StorageHealth health JSON field (application/storage_values.py)
total_bytes  # FALSE POSITIVE: StorageHealth health JSON field (application/storage_values.py)
free_bytes  # FALSE POSITIVE: StorageHealth health JSON field (application/storage_values.py)
observed_utc  # FALSE POSITIVE: _ApparentTransitionSag health JSON field (virtual_ups_exporter.py)

# Intentional public seams: these are called by sanctioned reset paths or
# preserve a durable contract that static reachability misses.
_.reset_baseline  # FALSE POSITIVE: sanctioned physical-battery reset API (adapters/model_owner.py)
trusted_prefixes  # FALSE POSITIVE: adapter-built persisted corruption/recovery contract (application/storage_values.py)
CLOSED_RESTART_GAP  # FALSE POSITIVE: TerminationFact value serialized in terminal records (domain/values.py)
ended_utc  # FALSE POSITIVE: public blackout/recharge history DTO field read by query consumers
comparison_available  # FALSE POSITIVE: public EventSummary field read by history consumers
ir_estimate_available  # FALSE POSITIVE: public EventSummary field read by history consumers
commit_receipt_id  # FALSE POSITIVE: public EventSummary field read by history consumers
