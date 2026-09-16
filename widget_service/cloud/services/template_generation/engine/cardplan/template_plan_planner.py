"""Deterministic planning between data-only Search and the second-layer LLM."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations, product

from models.generation import TaskSpec
from services.template_generation.engine.advanced.models import (
    AdvancedScopeBrief,
    TemplateComponentCandidate,
    TemplateRouteSelection,
)

from .business_actions import supports_business_action
from .models import (
    ActionBinding,
    TemplateDefinition,
    TemplatePlan,
    TemplatePlanActionAssignment,
    TemplatePlanBusinessSlot,
)
from .prompt import action_bindings
from .provider_bundle import provider_template_layout_kind
from .registry import CardPlanRegistry
from .template_retrieval import (
    TemplateBusinessCandidates,
    TemplateRetrievalMiss,
    TemplateSearchIntent,
    TemplateSearchResult,
)

_MAX_PLANS = 3
_MAX_WIDE_OPTIONS_PER_SLOT = 8
_PILL_ACTION_TEMPLATE_ID = "PillAction@1"
_ICON_ACTION_TEMPLATE_ID = "IconAction@1"
_COMPACT_ACTION_TEMPLATE_ID = "CompactAction@1"
_LARGE_ICON_ACTION_TEMPLATE_ID = "LargeIconAction@1"
_THEME_TERMS_BY_BUSINESS = {
    "ActivityOverview": ("sport", "activity", "运动", "步数"),
    "AppUsageOverview": ("app", "usage", "digital", "应用", "时长"),
    "BatteryOverview": ("battery", "device", "电量", "设备"),
    "BluetoothDeviceOverview": ("earphone", "audio", "battery", "耳机", "电量"),
    "CalendarOverview": ("calendar", "schedule", "meeting", "日历", "日程"),
    "CountdownOverview": ("countdown", "event", "倒计时"),
    "HeartRateOverview": ("sport", "heart", "rate", "运动", "心率"),
    "ResourceUsageOverview": ("device", "resource", "memory", "设备", "资源"),
    "SleepOverview": ("sleep", "睡眠"),
    "WeatherOverview": ("weather", "天气"),
    "WorkoutOverview": ("sport", "workout", "运动", "训练"),
}


@dataclass(frozen=True)
class _PlanDraft:
    plan: TemplatePlan
    score: tuple[int, ...]
    sequence: int


def plan_template_candidates(
    intent: TemplateSearchIntent,
    search_result: TemplateSearchResult,
    task_spec: TaskSpec,
    registry: CardPlanRegistry,
) -> tuple[TemplatePlan, ...]:
    """Build at most three complete, atomic UI plans from Search candidates."""
    if search_result.card_size != task_spec.size:
        raise TemplateRetrievalMiss("Search result card size does not match TaskSpec")
    groups_by_capability = _groups_by_capability(search_result)
    requested_capabilities = tuple(intent.required_output_fields_by_capability)
    if any(capability_id not in groups_by_capability for capability_id in requested_capabilities):
        raise TemplateRetrievalMiss("Search result does not cover every requested capability")
    action_ids = _selected_action_ids(intent, task_spec)
    drafts: list[_PlanDraft] = []
    sequence = 0
    group_options = tuple(groups_by_capability[item] for item in requested_capabilities)
    for selected_groups in product(*group_options):
        if len(selected_groups) == 1:
            new_drafts = _single_business_drafts(
                selected_groups[0],
                action_ids,
                intent,
                task_spec,
                registry,
            )
        elif len(selected_groups) == 2:
            new_drafts = _dual_business_drafts(
                selected_groups,
                action_ids,
                intent,
                task_spec,
                registry,
            )
        else:
            new_drafts = ()
        for plan, score in new_drafts:
            drafts.append(_PlanDraft(plan=plan, score=score, sequence=sequence))
            sequence += 1
    if not drafts:
        raise TemplateRetrievalMiss("Search candidates cannot form a supported atomic plan")

    if len(requested_capabilities) == 1:
        focus = intent.primary_output_field_by_capability.get(requested_capabilities[0])
        focused = [
            draft
            for draft in drafts
            if focus in draft.plan.business_slots[0].primary_matched_fields
        ]
        if focused:
            drafts = focused
    drafts.sort(key=lambda item: (*tuple(-value for value in item.score), item.sequence))
    deduplicated = _deduplicate_drafts(drafts)
    top_theme = deduplicated[0].plan.theme_id
    same_theme = [item for item in deduplicated if item.plan.theme_id == top_theme]
    return tuple(
        item.plan.model_copy(update={"plan_id": f"plan-{index + 1}"})
        for index, item in enumerate(same_theme[:_MAX_PLANS])
    )


def plan_wide_template_candidates(
    selection: TemplateRouteSelection,
    task_spec: TaskSpec,
    registry: CardPlanRegistry,
) -> tuple[TemplatePlan, ...]:
    """Enumerate complete 2x4 plans before the second-layer model call.

    The legacy 2x4 route chooses a layout from only the number of business
    components and Actions.  This planner keeps those layouts as the grammar,
    but evaluates concrete business-template assignments first so the second
    layer receives only complete atomic alternatives.
    """
    if task_spec.size != "2x4":
        return ()
    # The LLM route currently returns only component/template candidates.  It
    # has no trusted field-demand map for cross-slot coverage, so keep it on
    # the legacy route until that map is part of its validated selection.
    if not selection.required_output_fields_by_capability:
        return ()
    groups = selection.required_template_groups or tuple(
        candidate.available_template_ids for candidate in selection.component_candidates
    )
    if not groups:
        return ()
    # TemplateRouteSelection keeps the event IDs returned by the first layer,
    # while TemplatePlan/Compiler use the trusted internal ActionBinding IDs.
    # Resolve the former to the latter before creating root-action assignments.
    selected_event_ids = set(selection.action_ids)
    action_ids = tuple(
        action.action_id
        for action in action_bindings(task_spec)
        if action.event_id in selected_event_ids
    )
    if len(action_ids) != len(selected_event_ids):
        return ()
    # ``requiredTemplateGroups`` may contain repeated Generic Support slots,
    # while ``componentCandidates`` is intentionally de-duplicated.  Layout
    # arity follows the concrete groups, not the unique business IDs.
    patterns = _wide_layout_patterns(len(groups), len(action_ids))
    if not patterns:
        return ()

    candidates_by_id = {
        candidate.component_id: candidate for candidate in selection.component_candidates
    }
    component_capabilities = {
        component_id: _component_capability_id(
            component_id,
            selection.required_output_fields_by_capability,
            registry,
        )
        for component_id in candidates_by_id
    }
    drafts: list[_PlanDraft] = []
    sequence = 0
    seen_signatures: set[tuple[object, ...]] = set()
    for layout_id, roles, action_template_id in patterns:
        if len(roles) != len(groups):
            continue
        for group_order in _unique_group_orders(groups):
            ordered_groups = tuple(groups[index] for index in group_order)
            option_lists = tuple(
                _wide_slot_options(
                    ordered_group,
                    role,
                    selection.component_candidates,
                    component_capabilities,
                    selection.required_output_fields_by_capability,
                    registry,
                )
                for ordered_group, role in zip(ordered_groups, roles, strict=True)
            )
            if any(not options for options in option_lists):
                continue
            for options in product(*option_lists):
                if not _wide_options_have_distinct_businesses(options):
                    continue
                slots = _wide_plan_slots(
                    options,
                    selection.required_output_fields_by_capability,
                    registry,
                )
                if slots is None:
                    continue
                assignments = _root_action_assignments(action_ids, action_template_id)
                plan = _make_wide_plan(
                    layout_id,
                    slots,
                    assignments,
                    selection.scope.theme_id,
                    registry,
                )
                if plan is None:
                    continue
                signature = (
                    plan.layout_template_id,
                    tuple(slot.template_id for slot in plan.business_slots),
                    tuple(item.action_id for item in plan.action_assignments),
                )
                if signature in seen_signatures:
                    continue
                seen_signatures.add(signature)
                drafts.append(
                    _PlanDraft(
                        plan=plan,
                        score=_wide_plan_score(plan, registry),
                        sequence=sequence,
                    )
                )
                sequence += 1

    if not drafts:
        return ()
    drafts.sort(key=lambda item: (*tuple(-value for value in item.score), item.sequence))
    top_theme = drafts[0].plan.theme_id
    return tuple(
        item.plan.model_copy(update={"plan_id": f"wide-plan-{index + 1}"})
        for index, item in enumerate(
            draft for draft in drafts if draft.plan.theme_id == top_theme
        )
        if index < _MAX_PLANS
    )


@dataclass(frozen=True)
class _WideTemplateOption:
    component_id: str
    capability_id: str
    template_id: str
    role: str
    covered_fields: tuple[str, ...]


def _wide_layout_patterns(
    component_count: int,
    action_count: int,
) -> tuple[tuple[str, tuple[str, ...], str | None], ...]:
    patterns: dict[tuple[int, int], tuple[tuple[str, tuple[str, ...], str | None], ...]] = {
        (1, 0): (("WideFullOnlyLayout@1", ("WideFull",), None),),
        (1, 1): (("WideSingleFocusLayout@1", ("WideHero",), _PILL_ACTION_TEMPLATE_ID),),
        (1, 4): (
            (
                "WideFullFourActionLayout@1",
                ("Full",),
                _LARGE_ICON_ACTION_TEMPLATE_ID,
            ),
            (
                "WideHalfFourLargeActionLayout@1",
                ("WideHalf",),
                _LARGE_ICON_ACTION_TEMPLATE_ID,
            ),
        ),
        (2, 0): (
            ("WideTwoFullLayout@1", ("Full", "Full"), None),
            ("WideTwoHalfLayout@1", ("WideHalf", "WideHalf"), None),
            ("WideHeroSupportLayout@1", ("Hero", "Support"), None),
        ),
        (2, 1): (
            ("WideFullHeroActionLayout@1", ("Full", "Hero"), _PILL_ACTION_TEMPLATE_ID),
            ("WideHeroActionFullLayout@1", ("Full", "Hero"), _PILL_ACTION_TEMPLATE_ID),
            ("WideFullTwoSupportLayout@1", ("Full", "Support"), _COMPACT_ACTION_TEMPLATE_ID),
            ("WideFullTwoSupportLayout@1", ("Hero", "Support"), _COMPACT_ACTION_TEMPLATE_ID),
        ),
        (2, 2): (
            (
                "WideFullHeroTwoActionLayout@1",
                ("Full", "Hero"),
                _PILL_ACTION_TEMPLATE_ID,
            ),
            (
                "WideHalfSupportTwoLargeActionLayout@1",
                ("WideHalf", "Support"),
                _LARGE_ICON_ACTION_TEMPLATE_ID,
            ),
        ),
        (3, 0): (
            ("WideFullTwoSupportLayout@1", ("Full", "Support", "Support"), None),
            ("WideHalfTwoSupportLayout@1", ("WideHalf", "Support", "Support"), None),
        ),
        (4, 0): (("WideFourSupportLayout@1", ("Support",) * 4, None),),
    }
    return patterns.get((component_count, action_count), ())


def _unique_group_orders(groups: tuple[tuple[str, ...], ...]) -> tuple[tuple[int, ...], ...]:
    orders: list[tuple[int, ...]] = []
    seen: set[tuple[tuple[str, ...], ...]] = set()
    for order in permutations(range(len(groups))):
        signature = tuple(groups[index] for index in order)
        if signature in seen:
            continue
        seen.add(signature)
        orders.append(order)
    return tuple(orders)


def _component_capability_id(
    component_id: str,
    requested_fields: dict[str, tuple[str, ...]],
    registry: CardPlanRegistry,
) -> str:
    component = registry.require_ux_business_component(component_id)
    for capability_id in component.data_capability_ids:
        if capability_id in requested_fields:
            return capability_id
    if len(component.data_capability_ids) == 1:
        return component.data_capability_ids[0]
    raise TemplateRetrievalMiss(
        f"wide Template component has no requested capability: {component_id}"
    )


def _wide_slot_options(
    template_group: tuple[str, ...],
    role: str,
    candidates: tuple[TemplateComponentCandidate, ...],
    component_capabilities: dict[str, str],
    requested_fields: dict[str, tuple[str, ...]],
    registry: CardPlanRegistry,
) -> tuple[_WideTemplateOption, ...]:
    options: list[_WideTemplateOption] = []
    group_ids = set(template_group)
    for candidate in candidates:
        capability_id = component_capabilities[candidate.component_id]
        fields = requested_fields.get(capability_id, ())
        for template_id in candidate.available_template_ids:
            if template_id not in group_ids:
                continue
            if not _wide_role_matches(
                provider_template_layout_kind(template_id),
                role,
            ):
                continue
            definition = registry.require_template(template_id)
            declared_fields = set(
                (*definition.required_data, *definition.primary_data,
                 *definition.secondary_data, *definition.optional_data)
            )
            if candidate.component_id == "GenericMetricOverview":
                covered = tuple(fields)
            else:
                covered = tuple(path for path in fields if path in declared_fields)
            # A specialized business template may intentionally cover only a
            # subset of the requested fields; a Generic Support slot can carry
            # the residual fields.  Complete coverage is checked across all
            # slots by _wide_plan_slots below.
            if fields and not covered:
                continue
            options.append(
                _WideTemplateOption(
                    component_id=candidate.component_id,
                    capability_id=capability_id,
                    template_id=template_id,
                    role=role,
                    covered_fields=covered,
                )
            )
    options.sort(
        key=lambda item: (
            -len(item.covered_fields),
            item.component_id == "GenericMetricOverview",
            item.template_id,
        )
    )
    return tuple(options[:_MAX_WIDE_OPTIONS_PER_SLOT])


def _wide_role_matches(template_kind: str | None, expected_role: str) -> bool:
    """Match a business template to an explicitly declared wide slot role."""
    if template_kind is None:
        return False
    if expected_role == "WideFull":
        # WideFull is a distinct business-template shape. Do not silently
        # stretch a 2x2 Full template into this slot.
        return template_kind == "WideFull"
    if expected_role == "WideHero":
        return template_kind in {"WideHero", "Hero"}
    return template_kind == expected_role


def _wide_options_have_distinct_businesses(
    options: tuple[_WideTemplateOption, ...],
) -> bool:
    business_ids = [item.component_id for item in options]
    duplicates = {item for item in business_ids if business_ids.count(item) > 1}
    return duplicates <= {"GenericMetricOverview"}


def _wide_plan_slots(
    options: tuple[_WideTemplateOption, ...],
    requested_fields: dict[str, tuple[str, ...]],
    registry: CardPlanRegistry,
) -> tuple[TemplatePlanBusinessSlot, ...] | None:
    non_generic_coverage: dict[str, set[str]] = {}
    for option in options:
        if option.component_id != "GenericMetricOverview":
            non_generic_coverage.setdefault(option.capability_id, set()).update(
                option.covered_fields
            )
    generic_positions = [
        index
        for index, option in enumerate(options)
        if option.component_id == "GenericMetricOverview"
    ]
    generic_fields_by_position: dict[int, tuple[str, ...]] = {}
    for position in generic_positions:
        capability_id = options[position].capability_id
        requested = tuple(requested_fields.get(capability_id, ()))
        residual = tuple(
            path
            for path in requested
            if path not in non_generic_coverage.get(capability_id, set())
        )
        if not residual:
            residual = requested
        if len(generic_positions) == 1:
            generic_fields_by_position[position] = residual
            continue
        slot_index = generic_positions.index(position)
        if slot_index < len(residual):
            generic_fields_by_position[position] = (residual[slot_index],)
        else:
            generic_fields_by_position[position] = (residual[-1],) if residual else ()

    slots: list[TemplatePlanBusinessSlot] = []
    covered_by_capability: dict[str, set[str]] = {}
    for position, option in enumerate(options):
        covered = generic_fields_by_position.get(position, option.covered_fields)
        if not covered and requested_fields.get(option.capability_id):
            return None
        covered_by_capability.setdefault(option.capability_id, set()).update(covered)
        definition = registry.require_template(option.template_id)
        primary_matches = tuple(
            path for path in definition.primary_data if path in covered
        )
        slots.append(
            TemplatePlanBusinessSlot(
                position=position,
                businessId=option.component_id,
                capabilityId=option.capability_id,
                templateId=option.template_id,
                layoutRole=option.role,
                coveredExplicitFields=covered,
                primaryMatchedFields=primary_matches,
            )
        )
    for capability_id, fields in requested_fields.items():
        if not set(fields).issubset(covered_by_capability.get(capability_id, set())):
            return None
    return tuple(slots)


def _make_wide_plan(
    layout_template_id: str,
    slots: tuple[TemplatePlanBusinessSlot, ...],
    assignments: tuple[TemplatePlanActionAssignment, ...],
    theme_id: str,
    registry: CardPlanRegistry,
) -> TemplatePlan | None:
    for slot in slots:
        definition = registry.require_template(slot.template_id)
        if (
            definition.compatible_theme_profile_ids
            and theme_id not in definition.compatible_theme_profile_ids
        ):
            return None
    return TemplatePlan(
        planId="draft",
        themeId=theme_id,
        layoutTemplateId=layout_template_id,
        businessSlots=slots,
        actionAssignments=assignments,
    )


def _wide_plan_score(
    plan: TemplatePlan,
    registry: CardPlanRegistry,
) -> tuple[int, ...]:
    primary_matches = sum(len(slot.primary_matched_fields) for slot in plan.business_slots)
    specialized_slots = sum(
        slot.business_id != "GenericMetricOverview" for slot in plan.business_slots
    )
    role_weight = {"Full": 4, "WideFull": 4, "Hero": 3, "WideHero": 3,
                   "WideHalf": 2, "Support": 1}
    visual_score = sum(
        role_weight.get(slot.layout_role, 0) * (len(plan.business_slots) - index)
        for index, slot in enumerate(plan.business_slots)
    )
    optional_matches = 0
    for slot in plan.business_slots:
        definition = registry.require_template(slot.template_id)
        optional_matches += len(
            set(slot.covered_explicit_fields).intersection(definition.optional_data)
        )
    return (
        primary_matches,
        specialized_slots,
        visual_score,
        -optional_matches,
        -sum(slot.business_id == "GenericMetricOverview" for slot in plan.business_slots),
    )


def planner_scope(plans: tuple[TemplatePlan, ...]) -> AdvancedScopeBrief:
    """Project Planner results to the legacy scope needed by trusted prompt builders."""
    if not plans:
        raise ValueError("Template Planner produced no plan")
    first = plans[0]
    business_ids = tuple(dict.fromkeys(slot.business_id for slot in first.business_slots))
    plans_cover_same_businesses = all(
        _plan_business_ids(plan) == set(business_ids) for plan in plans
    )
    if not plans_cover_same_businesses:
        raise ValueError("Template Plans must cover the same businesses")
    if any(plan.theme_id != first.theme_id for plan in plans):
        raise ValueError("Template Plans must share one trusted Theme contract")
    return AdvancedScopeBrief(
        themeId=first.theme_id,
        advancedComponentIds=business_ids,
    )


def _plan_business_ids(plan: TemplatePlan) -> set[str]:
    return {slot.business_id for slot in plan.business_slots}


def planner_component_candidates(
    plans: tuple[TemplatePlan, ...],
) -> tuple[TemplateComponentCandidate, ...]:
    """Return the ordered candidate union used only to build prompt contracts."""
    scope = planner_scope(plans)
    template_ids_by_business: dict[str, list[str]] = {
        business_id: [] for business_id in scope.advanced_component_ids
    }
    for plan in plans:
        for slot in plan.business_slots:
            values = template_ids_by_business.get(slot.business_id)
            if values is None:
                raise ValueError("Template Plan contains an unknown business")
            if slot.template_id not in values:
                values.append(slot.template_id)
    candidates: list[TemplateComponentCandidate] = []
    for business_id in scope.advanced_component_ids:
        template_ids = template_ids_by_business.get(business_id)
        if template_ids is None:
            raise ValueError("Template Plan business has no candidate set")
        candidates.append(
            TemplateComponentCandidate(
                componentId=business_id,
                availableTemplateIds=tuple(template_ids),
            )
        )
    return tuple(candidates)


def planner_required_template_groups(
    plans: tuple[TemplatePlan, ...],
) -> tuple[tuple[str, ...], ...]:
    """Return one candidate group per concrete business slot.

    Generic Support is repeatable in a wide plan.  Keep those repeated slots
    in the prompt contract even though ``componentCandidates`` remains a
    de-duplicated business-component index.
    """
    first = plans[0]
    if not first.layout_template_id.startswith("Wide"):
        return tuple(
            candidate.available_template_ids
            for candidate in planner_component_candidates(plans)
        )
    groups: list[tuple[str, ...]] = []
    for position in range(len(first.business_slots)):
        template_ids: list[str] = []
        for plan in plans:
            if position >= len(plan.business_slots):
                continue
            template_id = plan.business_slots[position].template_id
            if template_id not in template_ids:
                template_ids.append(template_id)
        groups.append(tuple(template_ids))
    return tuple(groups)


def _groups_by_capability(
    search_result: TemplateSearchResult,
) -> dict[str, tuple[TemplateBusinessCandidates, ...]]:
    grouped: dict[str, list[TemplateBusinessCandidates]] = {}
    for group in search_result.business_candidates:
        grouped.setdefault(group.capability_id, []).append(group)
    return {capability_id: tuple(groups) for capability_id, groups in grouped.items()}


def _selected_action_ids(
    intent: TemplateSearchIntent,
    task_spec: TaskSpec,
) -> tuple[str, ...]:
    available_ids = {event.id for event in task_spec.eventCandidates if event.id}
    if not set(intent.action_ids).issubset(available_ids):
        raise TemplateRetrievalMiss("Planner Action is outside TaskSpec.eventCandidates")
    selected_ids = set(intent.action_ids)
    return tuple(
        action.action_id
        for action in action_bindings(task_spec)
        if action.event_id in selected_ids
    )


def _single_business_drafts(
    group: TemplateBusinessCandidates,
    action_ids: tuple[str, ...],
    intent: TemplateSearchIntent,
    task_spec: TaskSpec,
    registry: CardPlanRegistry,
) -> tuple[tuple[TemplatePlan, tuple[int, ...]], ...]:
    if task_spec.size != "2x2":
        return ()
    layouts: tuple[tuple[str, str, str | None], ...]
    if not action_ids:
        layouts = (("SingleFocusLayout@1", "Full", None),)
    elif len(action_ids) == 1:
        values = [("HeroActionLayout@1", "Hero", _PILL_ACTION_TEMPLATE_ID)]
        if _has_semantic_action_icon(task_spec):
            values.append(("FullIconActionLayout@1", "Full", _ICON_ACTION_TEMPLATE_ID))
        layouts = tuple(values)
    elif len(action_ids) == 2:
        layouts = (("CompactTwoActionLayout@1", "Compact", _PILL_ACTION_TEMPLATE_ID),)
    else:
        return ()
    result: list[tuple[TemplatePlan, tuple[int, ...]]] = []
    for layout_template_id, role, action_template_id in layouts:
        for candidate in group.candidates:
            if provider_template_layout_kind(candidate.template_id) != role:
                continue
            definition = registry.require_template(candidate.template_id)
            slot = _business_slot(0, group, candidate.template_id, role, intent, definition)
            assignments = _root_action_assignments(action_ids, action_template_id)
            plan = _make_plan(
                layout_template_id,
                (slot,),
                assignments,
                registry,
            )
            if plan is not None:
                result.append((plan, _plan_score(plan, intent, registry)))
    return tuple(result)


def _dual_business_drafts(
    groups: tuple[TemplateBusinessCandidates, TemplateBusinessCandidates],
    action_ids: tuple[str, ...],
    intent: TemplateSearchIntent,
    task_spec: TaskSpec,
    registry: CardPlanRegistry,
) -> tuple[tuple[TemplatePlan, tuple[int, ...]], ...]:
    if task_spec.size != "2x2" or len(action_ids) > 2:
        return ()
    bindings = action_bindings(task_spec)
    result: list[tuple[TemplatePlan, tuple[int, ...]]] = []
    ordered_groups = tuple(permutations(groups))
    if len(action_ids) == 1:
        for title_group, content_group in ordered_groups:
            title_ids = _template_ids_for_role(title_group, "HeroTitle")
            content_ids = _template_ids_for_role(content_group, "HeroContent")
            for title_id, content_id in product(title_ids, content_ids):
                slots = (
                    _business_slot(
                        0,
                        title_group,
                        title_id,
                        "HeroTitle",
                        intent,
                        registry.require_template(title_id),
                    ),
                    _business_slot(
                        1,
                        content_group,
                        content_id,
                        "HeroContent",
                        intent,
                        registry.require_template(content_id),
                    ),
                )
                plan = _make_plan(
                    "HeroTitleContentActionLayout@1",
                    slots,
                    _root_action_assignments(action_ids, _PILL_ACTION_TEMPLATE_ID),
                    registry,
                )
                if plan is not None:
                    result.append((plan, _plan_score(plan, intent, registry)))
    for first_group, second_group in ordered_groups:
        first_ids = _template_ids_for_role(first_group, "Support")
        second_ids = _template_ids_for_role(second_group, "Support")
        for first_id, second_id in product(first_ids, second_ids):
            slots = (
                _business_slot(
                    0,
                    first_group,
                    first_id,
                    "Support",
                    intent,
                    registry.require_template(first_id),
                ),
                _business_slot(
                    1,
                    second_group,
                    second_id,
                    "Support",
                    intent,
                    registry.require_template(second_id),
                ),
            )
            for assignments in _business_action_assignment_options(
                action_ids,
                slots,
                registry,
                task_spec.size,
                bindings,
            ):
                plan = _make_plan("TwoSupportLayout@1", slots, assignments, registry)
                if plan is not None:
                    result.append((plan, _plan_score(plan, intent, registry)))
    return tuple(result)


def _template_ids_for_role(
    group: TemplateBusinessCandidates,
    role: str,
) -> tuple[str, ...]:
    return tuple(
        candidate.template_id
        for candidate in group.candidates
        if provider_template_layout_kind(candidate.template_id) == role
    )


def _business_slot(
    position: int,
    group: TemplateBusinessCandidates,
    template_id: str,
    role: str,
    intent: TemplateSearchIntent,
    definition: TemplateDefinition,
) -> TemplatePlanBusinessSlot:
    candidate = next(item for item in group.candidates if item.template_id == template_id)
    focus = intent.primary_output_field_by_capability.get(group.capability_id)
    primary_matches = tuple(
        path
        for path in definition.primary_data
        if path in group.explicit_fields or path == focus
    )
    return TemplatePlanBusinessSlot(
        position=position,
        businessId=group.business_id,
        capabilityId=group.capability_id,
        templateId=template_id,
        layoutRole=role,
        coveredExplicitFields=candidate.covered_explicit_fields,
        primaryMatchedFields=primary_matches,
    )


def _root_action_assignments(
    action_ids: tuple[str, ...],
    action_template_id: str | None,
) -> tuple[TemplatePlanActionAssignment, ...]:
    if action_template_id is None:
        return ()
    return tuple(
        TemplatePlanActionAssignment(
            actionId=action_id,
            consumer="root-action",
            actionTemplateId=action_template_id,
        )
        for action_id in action_ids
    )


def _business_action_assignment_options(
    action_ids: tuple[str, ...],
    slots: tuple[TemplatePlanBusinessSlot, ...],
    registry: CardPlanRegistry,
    card_size: str,
    bindings: tuple[ActionBinding, ...],
) -> tuple[tuple[TemplatePlanActionAssignment, ...], ...]:
    if not action_ids:
        return ((),)
    bindings_by_id = {binding.action_id: binding for binding in bindings}
    position_options: list[tuple[int, ...]] = []
    for action_id in action_ids:
        action = bindings_by_id.get(action_id)
        if action is None:
            return ()
        eligible: list[int] = []
        for slot in slots:
            definition = registry.require_template(slot.template_id)
            if supports_business_action(definition, action, card_size):
                eligible.append(slot.position)
        if not eligible:
            return ()
        position_options.append(tuple(eligible))
    results: list[tuple[TemplatePlanActionAssignment, ...]] = []
    for positions in product(*position_options):
        if len(positions) != len(set(positions)):
            continue
        results.append(
            tuple(
                TemplatePlanActionAssignment(
                    actionId=action_id,
                    consumer="business-template",
                    businessPosition=position,
                )
                for action_id, position in zip(action_ids, positions, strict=True)
            )
        )
    return tuple(results)


def _make_plan(
    layout_template_id: str,
    slots: tuple[TemplatePlanBusinessSlot, ...],
    assignments: tuple[TemplatePlanActionAssignment, ...],
    registry: CardPlanRegistry,
) -> TemplatePlan | None:
    layout_id = layout_template_id.removesuffix("@1")
    theme_id = _resolve_theme(layout_id, slots, registry)
    if theme_id is None:
        return None
    return TemplatePlan(
        planId="draft",
        themeId=theme_id,
        layoutTemplateId=layout_template_id,
        businessSlots=slots,
        actionAssignments=assignments,
    )


def _resolve_theme(
    layout_id: str,
    slots: tuple[TemplatePlanBusinessSlot, ...],
    registry: CardPlanRegistry,
) -> str | None:
    if layout_id == "TwoSupportLayout":
        capability_ids = tuple(slot.capability_id for slot in slots)
        return registry.require_layout_theme(layout_id, capability_ids)
    owner = slots[1] if layout_id == "HeroTitleContentActionLayout" else slots[0]
    theme_ids = registry.first_layer_theme_ids((owner.business_id,))
    definitions = tuple(registry.require_template(slot.template_id) for slot in slots)
    compatible_theme_ids: list[str] = []
    for theme_id in theme_ids:
        compatible = all(
            not definition.compatible_theme_profile_ids
            or theme_id in definition.compatible_theme_profile_ids
            for definition in definitions
        )
        if compatible:
            compatible_theme_ids.append(theme_id)
    if not compatible_theme_ids:
        return None
    return max(
        compatible_theme_ids,
        key=lambda theme_id: _theme_business_score(
            theme_id,
            owner.business_id,
            registry,
        ),
    )


def _theme_business_score(
    theme_id: str,
    business_id: str,
    registry: CardPlanRegistry,
) -> int:
    theme = registry.require_theme(theme_id)
    theme_text = " ".join(
        (theme.theme_profile_id, theme.description, *theme.palette_scene_ids)
    ).casefold()
    terms = _THEME_TERMS_BY_BUSINESS.get(business_id, (business_id.casefold(),))
    return sum(term.casefold() in theme_text for term in terms)


def _plan_score(
    plan: TemplatePlan,
    intent: TemplateSearchIntent,
    registry: CardPlanRegistry,
) -> tuple[int, ...]:
    explicit_primary_matches = 0
    primary_matches = 0
    secondary_matches = 0
    optional_only_matches = 0
    for slot in plan.business_slots:
        definition = registry.require_template(slot.template_id)
        explicit = set(slot.covered_explicit_fields)
        focus = intent.primary_output_field_by_capability.get(slot.capability_id)
        if focus is not None and focus in definition.primary_data:
            explicit_primary_matches += 1
        primary_matches += len(explicit.intersection(definition.primary_data))
        secondary_matches += len(explicit.intersection(definition.secondary_data))
        optional_only_matches += len(explicit.intersection(definition.optional_data))
    return (
        explicit_primary_matches,
        primary_matches,
        secondary_matches,
        -optional_only_matches,
    )


def _deduplicate_drafts(drafts: list[_PlanDraft]) -> list[_PlanDraft]:
    result: list[_PlanDraft] = []
    seen: set[tuple[object, ...]] = set()
    for draft in drafts:
        plan = draft.plan
        signature = (
            plan.theme_id,
            plan.layout_template_id,
            tuple(slot.template_id for slot in plan.business_slots),
            tuple(
                (
                    item.action_id,
                    item.consumer,
                    item.business_position,
                    item.action_template_id,
                )
                for item in plan.action_assignments
            ),
        )
        if signature in seen:
            continue
        seen.add(signature)
        result.append(draft)
    return result


def _has_semantic_action_icon(task_spec: TaskSpec) -> bool:
    keywords = {"action", "event", "shortcut", "动作", "操作", "入口", "快捷"}
    for candidate in task_spec.assetCandidates:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("src"), str):
            continue
        text_values = [str(candidate.get("description", ""))]
        for key in ("sceneTags", "semanticTags", "tags"):
            values = candidate.get(key, ())
            if isinstance(values, list):
                text_values.extend(str(item) for item in values)
        normalized = " ".join(text_values).casefold()
        if any(keyword in normalized for keyword in keywords):
            return True
    return False
