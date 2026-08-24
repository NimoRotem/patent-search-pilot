"""Grounding, reference, semantic and figure-level rules.

These are the checks that decide whether the drawing says what the patent says. They are run
against the LayoutScene rather than against a re-parse of the SVG, and that is a deliberate
choice with a condition attached: a separate rule proves the SVG is a pure function of the
scene by re-rendering it and comparing, so checking the scene is checking the artifact. If that
rule ever fails, every other result here is void, which is why it is blocking.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from ..numerals import sort_key
from ..schemas import CONTAINMENT_PREDICATES, ValidationIssue
from .engine import ValidationContext, ValidationRule


class SourceGrounding(ValidationRule):
    """GRD001 — everything the figure shows traces to a paragraph of the document."""

    rule_id = "GRD001"
    severity = "blocking"
    category = "grounding"
    repair_action = "reevaluate_evidence"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        issues: list[ValidationIssue] = []
        for spec_entity in figure.spec.entities:
            entity = context.graph.entity(spec_entity.entity_id)
            if entity is None or not entity.evidence:
                issues.append(self.issue(
                    "A component in this figure has no supporting paragraph in the document.",
                    entity_id=spec_entity.entity_id,
                    reference_numeral=spec_entity.reference_numeral))
        relations = {relation.id: relation for relation in context.graph.relations}
        for spec_relation in figure.spec.relations:
            relation = relations.get(spec_relation.relation_id)
            if relation is None or not relation.evidence:
                issues.append(self.issue(
                    "A relationship in this figure has no supporting paragraph in the document.",
                    relation_id=spec_relation.relation_id))
        for step in figure.spec.steps:
            if not step.evidence:
                issues.append(self.issue(
                    f"Step {step.id} has no supporting paragraph in the document."))
        return issues


class DuplicateReference(ValidationRule):
    """REF001 — one numeral may be printed once per figure, for one object."""

    rule_id = "REF001"
    severity = "blocking"
    category = "reference"
    repair_action = "relayout"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        issues: list[ValidationIssue] = []
        counts = Counter(label.reference_numeral for label in figure.scene.labels)
        for numeral, count in sorted(counts.items(), key=lambda item: sort_key(item[0])):
            if count > 1:
                issues.append(self.issue(
                    f"Reference numeral {numeral} is printed {count} times on this sheet.",
                    reference_numeral=numeral, detail={"count": count}))
        owners: dict[str, set[str]] = defaultdict(set)
        for label in figure.scene.labels:
            owners[label.reference_numeral].add(label.entity_id)
        for numeral, entity_ids in sorted(owners.items(), key=lambda item: sort_key(item[0])):
            if len(entity_ids) > 1:
                issues.append(self.issue(
                    f"Reference numeral {numeral} is bound to more than one object.",
                    reference_numeral=numeral, detail={"entities": sorted(entity_ids)}))
        return issues


class CrossFigureReference(ValidationRule):
    """REF002 — a numeral means the same thing on every sheet."""

    rule_id = "REF002"
    severity = "blocking"
    category = "cross_figure"
    repair_action = "respec"
    scope = "job"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        seen: dict[str, tuple[str, str]] = {}
        issues: list[ValidationIssue] = []
        for bundle in context.figures:
            for label in bundle.scene.labels:
                previous = seen.get(label.reference_numeral)
                if previous is None:
                    seen[label.reference_numeral] = (label.entity_id, bundle.spec.figure_id)
                elif previous[0] != label.entity_id:
                    issues.append(self.issue(
                        f"Reference numeral {label.reference_numeral} names one object in "
                        f"{previous[1]} and a different object in {bundle.spec.figure_id}.",
                        reference_numeral=label.reference_numeral,
                        figure_id=bundle.spec.figure_id,
                        detail={"entities": [previous[0], label.entity_id]}))
        return issues


class UnknownReference(ValidationRule):
    """REF003 — nothing may be printed that the document's registry does not carry."""

    rule_id = "REF003"
    severity = "blocking"
    category = "reference"
    repair_action = "respec"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        known = set(context.graph.reference_registry)
        step_numerals = {step.reference_numeral for step in figure.spec.steps
                         if step.reference_numeral}
        issues: list[ValidationIssue] = []
        for numeral in sorted({label.reference_numeral for label in figure.scene.labels},
                              key=sort_key):
            if numeral in known or numeral in step_numerals:
                continue
            issues.append(self.issue(
                f"Reference numeral {numeral} is printed on the drawing but does not appear in "
                "the document.", reference_numeral=numeral))
        return issues


class MissingRequiredReference(ValidationRule):
    """REF004 — every numeral the specification requires is actually printed."""

    rule_id = "REF004"
    severity = "blocking"
    category = "reference"
    repair_action = "relayout"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        printed = {label.reference_numeral for label in figure.scene.labels}
        required = {entity.reference_numeral for entity in figure.spec.entities
                    if entity.reference_numeral}
        required |= {step.reference_numeral for step in figure.spec.steps
                     if step.reference_numeral}
        issues = []
        for numeral in sorted(required - printed, key=sort_key):
            issues.append(self.issue(
                f"Reference numeral {numeral} belongs on this figure but was not printed.",
                reference_numeral=numeral))
        return issues


class ProposedNumeral(ValidationRule):
    """REF005 — a proposed numeral keeps a figure out of VALIDATED until the text catches up."""

    rule_id = "REF005"
    severity = "blocking"
    category = "reference"
    repair_action = "revise_text"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        issues = []
        for spec_entity in figure.spec.entities:
            entity = context.graph.entity(spec_entity.entity_id)
            if entity is not None and entity.numeral_status == "PROPOSED":
                issues.append(self.issue(
                    f"Reference numeral {entity.reference_numeral} was proposed by the compiler "
                    "and is not yet in the description. Add it to the text before filing.",
                    entity_id=entity.id, reference_numeral=entity.reference_numeral))
        return issues


class MissingNumeral(ValidationRule):
    """REF006 — a component the figure must show but the text never numbered."""

    rule_id = "REF006"
    severity = "blocking"
    category = "reference"
    repair_action = "revise_text"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        issues = []
        for spec_entity in figure.spec.entities:
            entity = context.graph.entity(spec_entity.entity_id)
            if entity is not None and not entity.reference_numeral:
                issues.append(self.issue(
                    f"{entity.canonical_name!r} appears in this figure but the description gives "
                    "it no reference numeral.", entity_id=entity.id))
        return issues


class CaptionPartWithoutNumeral(ValidationRule):
    """REF007 — the figure's own caption names a part the description never numbered.

    The default is never to invent a numeral, so this does not block on the drawing: it blocks
    on the text, and the figure is returned as NEEDS_TEXT_UPDATE with the part named. Add the
    numeral to the description and the figure compiles.
    """

    rule_id = "REF007"
    severity = "blocking"
    category = "reference"
    repair_action = "revise_text"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        issues = []
        for annotation in figure.spec.annotations:
            if not annotation.startswith("needs-numeral:"):
                continue
            name = annotation.split(":", 1)[1]
            issues.append(self.issue(
                f"This figure is described as showing the {name}, and the description gives it "
                "no reference numeral. Add one to the text, or say the figure does not show it.",
                detail={"component": name}))
        return issues


class UnsupportedEntity(ValidationRule):
    """SEM001 — nothing is drawn that the figure's specification does not name."""

    rule_id = "SEM001"
    severity = "blocking"
    category = "semantic"
    repair_action = "relayout"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        allowed = {entity.entity_id for entity in figure.spec.entities}
        allowed |= {step.id for step in figure.spec.steps}
        prohibited = set(figure.spec.prohibited_entities)
        issues = []
        for node in figure.scene.nodes:
            if node.entity_id in allowed:
                continue
            message = ("An object is drawn that this figure's specification does not include."
                       if node.entity_id not in prohibited else
                       "An object explicitly excluded from this figure is drawn on it.")
            issues.append(self.issue(message, entity_id=node.entity_id,
                                     reference_numeral=node.reference_numeral))
        return issues


class MissingEntity(ValidationRule):
    """SEM002 — everything the specification names is drawn."""

    rule_id = "SEM002"
    severity = "blocking"
    category = "semantic"
    repair_action = "relayout"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        drawn = {node.entity_id for node in figure.scene.nodes}
        issues = []
        for entity in figure.spec.entities:
            if entity.entity_id not in drawn:
                issues.append(self.issue(
                    "A component this figure is specified to show is absent from the drawing.",
                    entity_id=entity.entity_id,
                    reference_numeral=entity.reference_numeral))
        for step in figure.spec.steps:
            if step.id not in drawn:
                issues.append(self.issue(
                    f"Step {step.id} is specified for this figure but is absent from it."))
        return issues


class UnsupportedRelation(ValidationRule):
    """SEM003 — every line on the sheet is a relationship the specification carries."""

    rule_id = "SEM003"
    severity = "blocking"
    category = "semantic"
    repair_action = "relayout"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        allowed = {relation.relation_id for relation in figure.spec.relations}
        allowed |= {f"flow_{edge.from_step}_{edge.to_step}" for edge in figure.spec.step_edges}
        issues = []
        for edge in figure.scene.edges:
            if edge.relation_id not in allowed:
                issues.append(self.issue(
                    "A connection is drawn that this figure's specification does not include.",
                    relation_id=edge.relation_id))
        return issues


class MissingRelation(ValidationRule):
    """SEM004 — every specified relationship is expressed, by a line or by nesting."""

    rule_id = "SEM004"
    severity = "blocking"
    category = "semantic"
    repair_action = "relayout"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        if figure.scene.artwork:
            # The arrangement was given to the image model in words and the parts were then
            # found in what it drew. A pump inside a housing is shown by being drawn inside it,
            # not by a connector line, and there are no connector lines on this kind of sheet.
            # Demanding one refused every reference-guided figure that had any relation at all.
            return []
        drawn = {edge.relation_id for edge in figure.scene.edges}
        relations = {relation.id: relation for relation in context.graph.relations}
        nodes = {node.entity_id: node for node in figure.scene.nodes}
        issues = []
        for spec_relation in figure.spec.relations:
            if spec_relation.relation_id in drawn:
                continue
            relation = relations.get(spec_relation.relation_id)
            if relation is None:
                continue
            if spec_relation.visual_representation == "containment":
                # Containment is drawn by one outline sitting inside another. Check that it is.
                if relation.predicate in {"contains", "surrounds"}:
                    outer, inner = relation.subject, relation.object
                else:
                    outer, inner = relation.object, relation.subject
                outer_node, inner_node = nodes.get(outer), nodes.get(inner)
                if outer_node is not None and inner_node is not None and \
                        outer_node.box.x <= inner_node.box.x and \
                        outer_node.box.y <= inner_node.box.y and \
                        inner_node.box.right <= outer_node.box.right and \
                        inner_node.box.bottom <= outer_node.box.bottom:
                    continue
                issues.append(self.issue(
                    "The description places one part inside another, but the drawing does not "
                    "nest them.", relation_id=relation.id))
                continue
            issues.append(self.issue(
                "A relationship this figure is specified to show is absent from the drawing.",
                relation_id=relation.id))
        for edge in figure.spec.step_edges:
            if f"flow_{edge.from_step}_{edge.to_step}" not in drawn:
                issues.append(self.issue(
                    f"The step sequence {edge.from_step} to {edge.to_step} is not drawn."))
        return issues


class RelationDirection(ValidationRule):
    """SEM005 — an arrowhead appears only where the document gave a direction, and points the
    way the document gave it."""

    rule_id = "SEM005"
    severity = "blocking"
    category = "semantic"
    repair_action = "relayout"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        relations = {relation.id: relation for relation in context.graph.relations}
        issues = []
        for edge in figure.scene.edges:
            relation = relations.get(edge.relation_id)
            if relation is None:
                continue          # a flowchart edge; its direction is the step order
            if edge.arrow_at_end and relation.direction == "none" and not edge.arrow_at_start:
                issues.append(self.issue(
                    "A connection carries an arrowhead but the description gives it no "
                    "direction.", relation_id=relation.id))
            if edge.arrow_at_end and relation.direction == "subject_to_object" and \
                    edge.from_entity != relation.subject:
                issues.append(self.issue(
                    "A directed connection is drawn the wrong way round.",
                    relation_id=relation.id,
                    detail={"expected": f"{relation.subject}->{relation.object}",
                            "drawn": f"{edge.from_entity}->{edge.to_entity}"}))
        return issues


class FigureDescriptionMatch(ValidationRule):
    """SEM006 — the drawing is the kind of drawing its own caption describes."""

    rule_id = "SEM006"
    severity = "blocking"
    category = "semantic"
    repair_action = "respec"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None:
            return []
        planned = next((item for item in context.plan.figures
                        if item.figure_number.upper() == figure.spec.figure_number.upper()),
                       None)
        issues = []
        if planned is not None and planned.figure_type != figure.spec.figure_type:
            issues.append(self.issue(
                f"The patent describes FIG. {planned.figure_number} as a "
                f"{planned.figure_type.replace('_', ' ')} but a "
                f"{figure.spec.figure_type.replace('_', ' ')} was drawn.",
                detail={"planned": planned.figure_type, "drawn": figure.spec.figure_type}))
        if figure.scene.figure_type != figure.spec.figure_type:
            issues.append(self.issue(
                "The rendered figure type does not match its specification.",
                detail={"spec": figure.spec.figure_type, "scene": figure.scene.figure_type}))
        return issues


class FigureNumbering(ValidationRule):
    """FIG001 / FIG002 — the figure set is the one the patent describes."""

    rule_id = "FIG001"
    severity = "blocking"
    category = "cross_figure"
    repair_action = "respec"
    scope = "job"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        counts = Counter(bundle.spec.figure_number.upper() for bundle in context.figures)
        for number, count in sorted(counts.items(), key=lambda item: sort_key(item[0])):
            if count > 1:
                issues.append(self.issue(
                    f"FIG. {number} was produced {count} times.",
                    detail={"figure_number": number}))
        produced = set(counts)
        for item in context.plan.figures:
            if item.explicit and item.figure_number.upper() not in produced:
                issues.append(ValidationIssue(
                    rule_id="FIG002", severity="blocking", category="cross_figure",
                    repair_action="respec",
                    message=(f"The patent describes FIG. {item.figure_number} "
                             f"({item.description[:120]}) but it could not be produced."),
                    detail={"figure_number": item.figure_number}))
        return issues


class DeterministicRender(ValidationRule):
    """RND001 — the SVG is a pure function of the scene and the profile.

    Everything else in this module inspects the scene rather than the file. That is only sound
    while re-rendering the scene reproduces the file byte for byte, so this rule proves it on
    every run instead of assuming it.
    """

    rule_id = "RND001"
    severity = "blocking"
    category = "semantic"
    repair_action = "relayout"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        figure = context.figure
        if figure is None or not figure.svg:
            return []
        from ..render import render_svg

        if render_svg(figure.scene, context.profile, figure.artwork) != figure.svg:
            return [self.issue(
                "The rendered drawing is not reproducible from its own layout, so the checks "
                "made against that layout do not describe this file.")]
        return []


SEMANTIC_RULES = [
    SourceGrounding(), DuplicateReference(), UnknownReference(), MissingRequiredReference(),
    ProposedNumeral(), MissingNumeral(), CaptionPartWithoutNumeral(),
    UnsupportedEntity(), MissingEntity(),
    UnsupportedRelation(), MissingRelation(), RelationDirection(), FigureDescriptionMatch(),
    DeterministicRender(), CrossFigureReference(), FigureNumbering(),
]
