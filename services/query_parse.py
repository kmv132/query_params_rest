from typing import Type

from sqlalchemy import asc, desc, and_, select, func, case
from sqlalchemy.orm import RelationshipDirection

from services.error import SQLGenerationException
from services.query_parser import SortOrder, ActionTree, NestedField, FilterAction
from services.serialization import BaseSerializer, get_prop_serializer

EXCLUDE_COLUMN_PREFIX = "!"

WILDCARD = "*"


def _debug_query(q):
    from sqlalchemy.dialects import sqlite

    print(q.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))


def _resolve_relationships(
    action: ActionTree, serializer: Type[BaseSerializer], id_field
):
    _model_inspect = serializer.get_model_inspection()
    _fields = []
    _joins = []
    for relation_name, relation_action_tree in action.relations.items():
        sql_relation = _model_inspect.relationships[relation_name]
        rel_serializer = get_prop_serializer(serializer.model, relation_name)
        _rel_cte = _relation_select(
            relation_action_tree,
            rel_serializer,
            serializer.model,
            sql_relation.primaryjoin,
        )
        if relation_action_tree.select is not None:
            _fields.append(relation_name)
            else_case = None
            match sql_relation.direction:
                case RelationshipDirection.ONETOMANY:
                    else_case = func.json("[]")
                    agg_fn = func.json(_rel_cte.c.obj)
                case RelationshipDirection.MANYTOONE:
                    agg_fn = func.json_extract(_rel_cte.c.obj, "$[0]")
                case _:
                    raise SQLGenerationException(
                        f"Unsupported relation type: {sql_relation.direction}"
                    )
            _fields.append(
                case(
                    (_rel_cte.c.id.is_not(None), agg_fn),
                    else_=else_case,
                )
            )
        _joins.append((relation_name, _rel_cte, id_field == _rel_cte.c.id))
    return _fields, _joins


def _json_query(qo: ActionTree, serializer: Type[BaseSerializer]):
    _fields = []
    _joins = []
    _hidden_fields_to_select = []
    _exclude_fields = []
    _model_inspect = serializer.get_model_inspection()
    _wild_select = any((_field == "*" for _field in qo.select))
    _field_to_select = []
    if _wild_select:
        _field_to_select = [
            _field
            for _field in serializer.fields
            if _field not in _model_inspect.relationships
        ]
    for _field in qo.select:
        if _field.startswith("!"):
            _field_to_select.remove(_field[1:])

    for field in _field_to_select:
        _fields.append(field)
        _fields.append(serializer.get_field(field))
    if "id" not in qo.select:
        _hidden_fields_to_select.append(serializer.get_field("id"))
    _filters = []
    _inner_cte: list[str] = []
    for flt_item in qo.filters:
        if isinstance(flt_item.field, NestedField):
            if flt_item.field.fields[0] in qo.relations:
                rel_action = qo.relations[flt_item.field.fields[0]]
            else:
                rel_action = ActionTree()
                rel_action.select = None
                qo.relations[flt_item.field.fields[0]] = rel_action

            rel_action.filters.append(
                FilterAction(
                    field=flt_item.field.shift_down(),
                    op=flt_item.operator,
                    value=flt_item.value,
                )
            )
            _inner_cte.append(flt_item.field.fields[0])
            continue
        _filters.append(
            flt_item.operator(serializer.get_field(flt_item.field), flt_item.value)
        )
    rel_fields, _joins = _resolve_relationships(qo, serializer, serializer.model.id)
    _fields.extend(rel_fields)

    for relation_name, relation_action_tree in qo.relations.items():
        sql_relation = _model_inspect.relationships[relation_name]

        if sql_relation.primaryjoin.left in _model_inspect.columns.values():
            this_id_col = sql_relation.primaryjoin.left
        else:
            this_id_col = sql_relation.primaryjoin.right
        has_child_id_col = this_id_col != serializer.model.id

        if has_child_id_col:
            _hidden_fields_to_select.append(this_id_col)
    obj = func.json_object(*_fields)
    q = select(obj.label("sql_rest"), *_hidden_fields_to_select)
    for join in _joins:
        match join:
            case (relation_name, cte, on_clause):
                q = q.join(
                    cte, onclause=on_clause, isouter=relation_name not in _inner_cte
                )

    if _filters:
        q = q.filter(*_filters)
    if qo.sort is not None:
        q = q.order_by(
            asc(serializer.get_field(qo.sort.field))
            if qo.sort.order is not SortOrder.DESC
            else desc(serializer.get_field(qo.sort.field))
        )
    if qo.offset:
        q = q.offset(qo.offset)
    if qo.limit:
        q = q.limit(qo.limit)
    q = q.group_by(serializer.get_field("id"))
    q = q.subquery()

    return q


def _relation_select(
    action: ActionTree,
    serializer: Type[BaseSerializer],
    parent_model,
    primaryjoin,
):
    fields_into_json = []
    _joins = []
    _cte = None
    _model_inspect = serializer.get_model_inspection()
    if primaryjoin.left in _model_inspect.columns.values():
        parent_id_col = primaryjoin.left
        other_id_col = primaryjoin.right
    else:
        parent_id_col = primaryjoin.right
        other_id_col = primaryjoin.left
    has_parent_id_col = parent_id_col != serializer.model.id
    if action.select:
        _exclude_fields = []

        _wild_select = any((_field == WILDCARD for _field in action.select))
        _field_to_select = []
        if _wild_select:
            _field_to_select = [
                _field
                for _field in serializer.fields
                if _field not in _model_inspect.relationships
            ]
        for _field in action.select:
            if _field.startswith(EXCLUDE_COLUMN_PREFIX):
                _field_to_select.remove(_field[1:])

        fld = set(serializer.get_field(field) for field in _field_to_select)
        if has_parent_id_col:
            fld.add(parent_id_col)
        for flt in action.filters:
            if isinstance(flt.field, NestedField):
                continue
            fld.add(serializer.get_field(flt.field))
        fld.add(serializer.get_field("id"))
        q = select(*fld)
    else:
        q = select(serializer.model)
        _field_to_select = [
            _field
            for _field in serializer.fields
            if _field not in _model_inspect.relationships
        ]

    if action.sort is not None:
        col = serializer.get_field(action.sort.field)
        col = desc(col) if action.sort.order == SortOrder.DESC else asc(col)
        q = q.order_by(col)
    q = q.subquery()

    for field in _field_to_select or []:
        fields_into_json.append(field)
        fields_into_json.append(q.c[field])

    filter_items = []
    _inner_cte: list[str] = []
    for flt_item in action.filters:
        if isinstance(flt_item.field, NestedField):
            if flt_item.field.fields[0] in action.relations:
                rel_action = action.relations[flt_item.field.fields[0]]
            else:
                rel_action = ActionTree()
                rel_action.select = ["id"]
                action.relations[flt_item.field.fields[0]] = rel_action

            rel_action.filters.append(
                FilterAction(
                    field=flt_item.field.shift_down(),
                    op=flt_item.operator,
                    value=flt_item.value,
                )
            )
            _inner_cte.append(flt_item.field.fields[0])
            continue
        filter_items.append(flt_item.operator(q.c[flt_item.field], flt_item.value))

    relation_fields_into_json, _joins = _resolve_relationships(
        action,
        serializer,
        q.c.id,
    )
    fields_into_json.extend(relation_fields_into_json)

    _cte = select(
        func.json_group_array(func.json_object(*fields_into_json)).label("obj"),
        parent_model.id.label("id")
        if not has_parent_id_col
        else q.c[parent_id_col.name].label("id"),
    ).select_from(q)

    if not has_parent_id_col:
        _cte = _cte.join(
            parent_model,
            onclause=other_id_col == q.c[parent_id_col.name],
            isouter=True,
        )

    for relation_name, rel_cte, onclause in _joins:
        _cte = _cte.join(
            rel_cte,
            onclause=onclause,
            isouter=relation_name not in _inner_cte,
        )
    if filter_items:
        _cte = _cte.filter(and_(*filter_items))
    _cte = _cte.group_by(
        parent_model.id if not has_parent_id_col else q.c[parent_id_col.name]
    )

    return _cte.cte().prefix_with("NOT MATERIALIZED")


def get_all(query_options: ActionTree, serializer):
    query = select(
        "["
        + func.coalesce(
            func.group_concat(_json_query(query_options, serializer).c.sql_rest), ""
        )
        + "]"
    )
    return query
