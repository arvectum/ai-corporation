from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI

from src.modules.action_queue.router import router as action_queue_router
from src.modules.agent_registry.router import router as agent_registry_router
from src.modules.agent_registry.internal_router import router as internal_company_agents_router
from src.modules.hermes_agent.internal_router import router as hermes_agent_router
from src.modules.action_console.router import router as action_console_router
from src.modules.acceptance_control.router import router as acceptance_control_router
from src.modules.archive_export.router import router as archive_export_router
from src.modules.connector_registry.router import router as connector_registry_router
from src.modules.copilot_feed.router import router as copilot_feed_router
from src.modules.bid_completeness.router import router as bid_completeness_router
from src.modules.bid_documents.router import router as bid_documents_router
from src.modules.bid_packages.router import router as bid_packages_router
from src.modules.cash_gap.router import router as cash_gap_router
from src.modules.ceo_approval.router import router as ceo_approval_router
from src.modules.claim_triggers.router import router as claim_triggers_router
from src.modules.deal_closure_reports.router import router as deal_closure_reports_router
from src.modules.closing_docs.router import router as closing_docs_router
from src.modules.commercial_prebid_demo.router import router as commercial_prebid_demo_router
from src.modules.commercial_operator_console.router import router as commercial_operator_console_router
from src.modules.commercial_bid_readiness.router import router as commercial_bid_readiness_router
from src.modules.compliance_matrix.router import router as compliance_matrix_router
from src.modules.contract_risks.router import router as contract_risks_router
from src.modules.contract_negotiation.router import router as contract_negotiation_router
from src.modules.cost_model.router import router as cost_model_router
from src.modules.customer_registry.router import router as customer_registry_router
from src.modules.customer_pilot.router import router as customer_pilot_router
from src.modules.dashboard_snapshots.router import router as dashboard_snapshots_router
from src.modules.deal_registry.router import router as deals_router
from src.modules.deal_closure.router import router as deal_closure_router
from src.modules.delivery_launch.router import router as delivery_launch_router
from src.modules.delivery_milestones.router import router as delivery_milestones_router
from src.modules.document_store.router import router as artifacts_router
from src.modules.document_ingestion.router import router as document_ingestion_router
from src.modules.document_requirements.router import router as document_requirements_router
from src.modules.event_log.router import router as event_log_router
from src.modules.execution_command.router import router as execution_command_router
from src.modules.execution_plans.router import router as execution_plans_router
from src.modules.execution_ledger.router import router as execution_ledger_router
from src.modules.external_execution.router import router as external_execution_router
from src.modules.finance_memo.router import router as finance_memo_router
from src.modules.financing_strategy.router import router as financing_strategy_router
from src.modules.initial_tech_risks.router import router as initial_tech_risks_router
from src.modules.incident_register.router import router as incident_register_router
from src.modules.incidents.router import router as incidents_router
from src.modules.intake_priority.router import router as intake_priority_router
from src.modules.integration_tasks.router import router as integration_tasks_router
from src.modules.integrated_risk_memo.router import router as integrated_risk_memo_router
from src.modules.kpi_learning.router import router as kpi_learning_router
from src.modules.knowledge_assets.router import router as knowledge_assets_router
from src.modules.learning_automation.router import router as learning_automation_router
from src.modules.launch_visibility.router import router as launch_visibility_router
from src.modules.logistics_tracking.router import router as logistics_tracking_router
from src.modules.priority_scoring.router import router as priority_scoring_router
from src.modules.postmortems.router import router as postmortems_router
from src.modules.post_submission.router import router as post_submission_router
from src.modules.prompt_schema_library.router import router as prompt_schema_library_router
from src.modules.procedure_monitor.router import router as procedure_monitor_router
from src.modules.purchase_orders.router import router as purchase_orders_router
from src.modules.outcome_intake.router import router as outcome_intake_router
from src.modules.optimization.router import router as optimization_router
from src.modules.operator_sessions.router import router as operator_sessions_router
from src.modules.payment_collection.router import router as payment_collection_router
from src.modules.payment_tracking.router import router as payment_tracking_router
from src.modules.quote_comparison.router import router as quote_comparison_router
from src.modules.quote_repository.router import router as quote_repository_router
from src.modules.requirement_extraction.router import router as requirement_extraction_router
from src.modules.rfq_generator.router import router as rfq_generator_router
from src.modules.runtime_control_traces.router import router as runtime_control_traces_router
from src.modules.runtime_metadata_slices.router import router as runtime_metadata_slices_router
from src.modules.shipping_acceptance.router import router as shipping_acceptance_router
from src.modules.status_engine.router import router as status_router
from src.modules.submission_readiness.router import router as submission_readiness_router
from src.modules.submission_control.router import router as submission_control_router
from src.modules.submission_archive.router import router as submission_archive_router
from src.modules.submission_receipts.router import router as submission_receipts_router
from src.modules.supplier_communications.router import router as supplier_communications_router
from src.modules.supplier_contracts.router import router as supplier_contracts_router
from src.modules.supplier_fulfillment.router import router as supplier_fulfillment_router
from src.modules.supplier_ratings.router import router as supplier_ratings_router
from src.modules.supplier_progress.router import router as supplier_progress_router
from src.modules.supplier_registry.router import router as supplier_registry_router
from src.modules.supplier_search.router import router as supplier_search_router
from src.modules.supplier_verification.router import router as supplier_verification_router
from src.modules.tender_screening.router import router as tender_screening_router
from src.modules.tender_import.router import router as tender_import_router
from src.modules.tender_intake.router import router as tender_intake_router
from src.modules.tender_operator_agent_demo.router import router as tender_operator_agent_demo_router
from src.tender_research.api import router as tender_research_router
from src.modules.tender_normalization.router import router as tender_normalization_router
from src.modules.tender_summary.router import router as tender_summary_router
from src.modules.vendor_connectors.router import router as vendor_connectors_router
from src.modules.workflow_runs.router import router as workflow_runs_router
from src.modules.workspace_feed.router import router as workspace_feed_router
from src.shared.api.errors import register_exception_handlers
from src.shared.api.middleware import install_runtime_middlewares
from src.shared.api.site_mount import install_optional_site_mount
from src.shared.redis.client import close_client, health_snapshot
from src.shared.storage.capacity import storage_metrics_dict
from src.shared.config.settings import get_settings

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    close_client()


app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)
install_runtime_middlewares(app, settings)
register_exception_handlers(app)

app.include_router(deals_router)
app.include_router(agent_registry_router)
app.include_router(internal_company_agents_router)
app.include_router(hermes_agent_router)
app.include_router(dashboard_snapshots_router)
app.include_router(archive_export_router)
app.include_router(workflow_runs_router)
app.include_router(optimization_router)
app.include_router(copilot_feed_router)
app.include_router(connector_registry_router)
app.include_router(workspace_feed_router)
app.include_router(action_queue_router)
app.include_router(action_console_router)
app.include_router(acceptance_control_router)
app.include_router(customer_registry_router)
app.include_router(customer_pilot_router)
app.include_router(integration_tasks_router)
app.include_router(intake_priority_router)
app.include_router(operator_sessions_router)
app.include_router(execution_ledger_router)
app.include_router(external_execution_router)
app.include_router(deal_closure_router)
app.include_router(delivery_launch_router)
app.include_router(delivery_milestones_router)
app.include_router(status_router)
app.include_router(artifacts_router)
app.include_router(event_log_router)
app.include_router(execution_command_router)
app.include_router(execution_plans_router)
app.include_router(tender_import_router)
app.include_router(tender_intake_router)
app.include_router(tender_operator_agent_demo_router)
app.include_router(tender_normalization_router)
app.include_router(document_ingestion_router)
app.include_router(requirement_extraction_router)
app.include_router(tender_summary_router)
app.include_router(vendor_connectors_router)
app.include_router(tender_screening_router)
app.include_router(priority_scoring_router)
app.include_router(compliance_matrix_router)
app.include_router(document_requirements_router)
app.include_router(initial_tech_risks_router)
app.include_router(incidents_router)
app.include_router(supplier_registry_router)
app.include_router(supplier_search_router)
app.include_router(rfq_generator_router)
app.include_router(supplier_communications_router)
app.include_router(quote_repository_router)
app.include_router(supplier_verification_router)
app.include_router(quote_comparison_router)
app.include_router(cost_model_router)
app.include_router(cash_gap_router)
app.include_router(closing_docs_router)
app.include_router(claim_triggers_router)
app.include_router(deal_closure_reports_router)
app.include_router(commercial_prebid_demo_router)
app.include_router(commercial_operator_console_router)
app.include_router(commercial_bid_readiness_router)
app.include_router(postmortems_router)
app.include_router(supplier_ratings_router)
app.include_router(knowledge_assets_router)
app.include_router(launch_visibility_router)
app.include_router(financing_strategy_router)
app.include_router(finance_memo_router)
app.include_router(contract_risks_router)
app.include_router(contract_negotiation_router)
app.include_router(integrated_risk_memo_router)
app.include_router(kpi_learning_router)
app.include_router(learning_automation_router)
app.include_router(logistics_tracking_router)
app.include_router(ceo_approval_router)
app.include_router(bid_documents_router)
app.include_router(bid_packages_router)
app.include_router(bid_completeness_router)
app.include_router(submission_readiness_router)
app.include_router(submission_archive_router)
app.include_router(submission_control_router)
app.include_router(submission_receipts_router)
app.include_router(post_submission_router)
app.include_router(prompt_schema_library_router)
app.include_router(runtime_control_traces_router)
app.include_router(runtime_metadata_slices_router)
app.include_router(procedure_monitor_router)
app.include_router(supplier_contracts_router)
app.include_router(purchase_orders_router)
app.include_router(outcome_intake_router)
app.include_router(payment_collection_router)
app.include_router(payment_tracking_router)
app.include_router(shipping_acceptance_router)
app.include_router(incident_register_router)
app.include_router(supplier_fulfillment_router)
app.include_router(supplier_progress_router)
app.include_router(tender_research_router)


@app.get("/health")
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def readiness() -> dict[str, object]:
    data_dir = Path(settings.arvectum_data_dir)
    writable = data_dir.exists() and data_dir.is_dir()
    storage_metrics = storage_metrics_dict()
    redis_health = health_snapshot()
    redis_ready = redis_health.get("status") == "healthy"
    redis_required = settings.arvectum_redis_enabled
    customer_pilot_ready = not redis_required or redis_ready
    overall_status = "ok"
    if not writable or not storage_metrics.get("ingestion_allowed", False):
        overall_status = "degraded"
    elif redis_required and not redis_ready:
        overall_status = "degraded"
    return {
        "status": overall_status,
        "data_writable": writable,
        "storage": storage_metrics,
        "redis": {
            "enabled": redis_health["enabled"],
            "status": redis_health["status"],
            "latency_ms": redis_health["latency_ms"],
            "error_category": redis_health["error_category"],
        },
        "feature_readiness": {
            "customer_pilot_run_start": "ready" if customer_pilot_ready else "blocked",
        },
        "timestamp": datetime.now(UTC).isoformat(),
    }


install_optional_site_mount(app, settings)
