from __future__ import annotations
from typing import Any,Mapping
from qrgf_common import clamp,tolerant_bool,tolerant_float,parse_datetime


def normalize_evidence_records(raw:Any):
    if isinstance(raw,Mapping):
        rows=[]
        for key,value in raw.items():
            if isinstance(value,Mapping): rows.append({'evidence_id':key,**dict(value)})
    elif isinstance(raw,list): rows=raw
    else: return [],['evidence must be an array or object']
    normalized=[];errors=[];seen=set();valid_quality={'verified','usable','conflict','stale','invalid','missing'}
    for index,value in enumerate(rows):
        if not isinstance(value,Mapping): errors.append(f'evidence[{index}] must be an object');continue
        row=dict(value);ident=str(row.get('evidence_id') or row.get('id') or '').strip()
        if not ident: errors.append(f'evidence[{index}] missing evidence_id');continue
        if ident in seen: errors.append(f'duplicate evidence_id {ident}');continue
        seen.add(ident);field=str(row.get('field') or '').strip()
        if not field: errors.append(f'evidence {ident} missing field')
        if 'value' not in row: errors.append(f'evidence {ident} missing value')
        source_value=row.get('source') or row.get('source_id')
        if isinstance(source_value,Mapping):
            source_id=str(source_value.get('source_id') or '').strip();source_type=str(source_value.get('source_type') or 'other').strip().lower();source=dict(source_value)
        else:
            source_id=str(source_value or '').strip();source_type=str(row.get('source_type') or 'other').strip().lower();source={'source_id':source_id,'source_type':source_type}
        if not source_id: errors.append(f'evidence {ident} missing source')
        for date_field in ('as_of','retrieved_at'):
            try:
                parsed=parse_datetime(row.get(date_field))
                if parsed is None: raise ValueError('missing')
            except ValueError: errors.append(f'evidence {ident} invalid or missing {date_field}')
        quality=str(row.get('quality_status') or '').strip().lower()
        if quality not in valid_quality: errors.append(f'evidence {ident} invalid or missing quality_status')
        elif quality not in {'verified','usable'}: errors.append(f'evidence {ident} is not usable: {quality}')
        normalized.append({**row,'evidence_id':ident,'field':field,'source':source,'quality_status':quality})
    return normalized,errors


def evidence_ids_for_field(records:list[dict[str,Any]],field:str)->list[str]:
    expected=str(field or '').strip()
    if not expected:return []
    parts=expected.split('.');result=[]
    for row in records:
        if str(row.get('quality_status') or '').lower() not in {'verified','usable'}:continue
        observed=str(row.get('field') or '').strip();covered=observed==expected
        if not covered and len(parts)>1 and observed==parts[0]:
            value=row.get('value');covered=True
            for part in parts[1:]:
                if not isinstance(value,Mapping) or part not in value:covered=False;break
                value=value.get(part)
        if covered:result.append(str(row.get('evidence_id')))
    return sorted(set(result))


def required_evidence_links(records:list[dict[str,Any]],fields)->tuple[dict[str,list[str]],list[str]]:
    links={};missing=[]
    for field in sorted({str(value) for value in fields if str(value).strip()}):
        ids=evidence_ids_for_field(records,field);links[field]=ids
        if not ids:missing.append(field)
    return links,missing
