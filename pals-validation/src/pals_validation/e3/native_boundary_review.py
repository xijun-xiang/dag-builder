"""Exploratory layout review, NOT a scoring parser or a protocol promotion.

Keep scored tag-based segments exact. For other rows, inventory explicit native
line boundaries and lossless candidate partitions. Lists can enumerate premises
or options rather than reasoning: structural candidates need content review.
Never infer answer boundaries, split sentences, execute code, or return g values.
"""
from collections import defaultdict
import re

from .greedy_parse import parse as parse_v2
from .greedy_parse_v3 import parse as parse_v3
from .schema import require

VERSION = 'native-layout-review-v1'
POLICY = {
    'preserve_existing_multistep': True,
    'named_step_numbers': 'line-start Step N or markdown Step N; consecutive 1..K',
    'ordered_numbers': 'top-level line-start N. or N); consecutive 1..K',
    'headings': 'same-level Markdown heading or standalone bold heading',
    'protected_regions': 'fenced code and display math; nested lists not promoted',
    'prefix': 'preserve introduction inside first candidate, never invent an intro step',
    'answer': 'retain existing v3 answer/process policy; missing boundaries remain blocked',
    'promotion': 'none; offline structural review only',
}
NAMED = re.compile(r'^(?:#{1,6}\s+)?(?:\*\*)?Step\s+(\d+)\s*[:.\-]\s*(.+)', re.I)
ORDERED = re.compile(r'^(\d+)[.)]\s+(.+)')
HEADING = re.compile(r'^(#{1,6})\s+(\S.*)')
BOLD = re.compile(r'^\*\*([^\n]+)\*\*\s*$')


def markers(raw: str, start: int, end: int) -> tuple[list, dict]:
    """Absolute character offsets; no mutation of the actual output."""
    result=[]; offset=start; fence=None; math=None; code_blocks=0
    for line in raw[start:end].splitlines(keepends=True):
        stripped=line.strip(); indent=len(line)-len(line.lstrip(' '))
        if fence:
            if re.fullmatch(re.escape(fence[0])+r'{'+str(fence[1])+r',}\s*', stripped):
                fence=None
            offset+=len(line);continue
        if math:
            if math in stripped:math=None
            offset+=len(line);continue
        fm=re.match(r'^(`{3,}|~{3,})',stripped)
        if fm:
            fence=(fm[1][0],len(fm[1]));code_blocks+=1
            offset+=len(line);continue
        for opening,closing in ((r'\[',r'\]'),('$$','$$')):
            if stripped.startswith(opening):
                if closing not in stripped[len(opening):]:math=closing
                break
        else:
            if indent<=3 and not line.startswith('\t'):
                text=line.strip()
                # A tag at the very beginning of the region is not a new native marker.
                named=NAMED.match(text);ordered=ORDERED.match(text)
                heading=HEADING.match(text);bold=BOLD.match(text)
                kind=None;number=None
                if named:kind='named_step';number=int(named[1])
                elif ordered:kind='ordered_list';number=int(ordered[1])
                elif heading:kind='heading_h'+str(len(heading[1]))
                elif bold:kind='bold_heading'
                if kind:
                    a=offset+indent;b=offset+len(line.rstrip('\r\n'))
                    result.append({'kind':kind,'number':number,'span':[a,b],'text':raw[a:b]})
        offset+=len(line)
    return result,{'code_fences':code_blocks,'unclosed_fence':fence is not None,
                   'unclosed_display_math':math is not None}


def inspect(raw: str, finish: str, benchmark: str) -> dict:
    old=parse_v2(raw,finish,benchmark);base=parse_v3(raw,finish,benchmark)
    v2_count=len(old['steps']) if old['process_valid'] else 0
    v3_count=len(base['steps']) if base['process_valid'] else 0
    common={'version':VERSION,'v2_valid':old['process_valid'],'v2_scoreable':v2_count>=2,
            'v3_valid':base['process_valid'],'v3_scoreable':v3_count>=2,
            'v3_step_count':v3_count,'v3_reason':base['reason'],
            'formal_eligible':False,'candidate_partitions':[], 'native_candidates':False,
            'candidate_with_existing_process_boundary':False,
            'candidate_without_code_mixture':False,
            'explicit_step_open_count':raw.count('<step>'),
            'answer_open_count':raw.count('<answer>')}
    if v3_count>=2:
        return {**common,'decision':'preserve_existing_multistep','steps':base['steps']}
    if v3_count==1:
        start,end=base['steps'][0]['span']
    else:
        start=raw.find('<step>')+6 if raw.lstrip().startswith('<step>') else 0
        end=raw.find('<answer>') if raw.count('<answer>')==1 else len(raw)
        if raw[start:end].rstrip().endswith('</step>'):
            end=raw.rfind('</step>',start,end)
    require(0<=start<=end<=len(raw),'invalid review region')
    found,protected=markers(raw,start,end)
    grouped=defaultdict(list)
    for marker in found:grouped[marker['kind']].append(marker)
    candidates=[]
    for kind,group in grouped.items():
        if len(group)<2:continue
        reasons=[];cautions=[]
        numbers=[m['number'] for m in group]
        if kind in ('named_step','ordered_list') and numbers!=list(range(1,len(group)+1)):
            reasons.append('number_sequence_restarts_or_has_gaps')
        if protected['unclosed_fence'] or protected['unclosed_display_math']:
            reasons.append('unclosed_protected_region')
        if protected['code_fences']:cautions.append('code_or_examples_inside_process')
        if kind=='ordered_list':cautions.append('list_may_enumerate_facts_or_cases')
        if kind.startswith('heading') or kind=='bold_heading':
            cautions.append('heading_may_label_options_or_report_sections')
        boundaries=[start]+[m['span'][0] for m in group[1:]]+[end]
        spans=[[a,b] for a,b in zip(boundaries,boundaries[1:])]
        texts=[raw[a:b] for a,b in spans]
        require(''.join(texts)==raw[start:end],'candidate dropped or rewrote text')
        if any(not text.strip() for text in texts):reasons.append('empty_candidate')
        candidates.append({'kind':kind,'markers':group,'steps':[{'span':s,'block_span':s,'text':t}
                          for s,t in zip(spans,texts)],'partition_valid':not reasons,
                          'reasons':reasons,'cautions':cautions})
    good=[c for c in candidates if c['partition_valid']]
    missing_boundary=not base['process_valid']
    # Do not override existing truncation/answer/tail checks simply to raise coverage.
    boundary_compatible=bool(good and not missing_boundary)
    return {**common,'decision':'review_native_candidates' if good else 'no_unambiguous_native_sequence',
            'region':[start,end],'markers':found,'protected':protected,
            'candidate_partitions':candidates,'native_candidates':bool(good),
            'candidate_with_existing_process_boundary':boundary_compatible,
            'candidate_without_code_mixture':boundary_compatible and not protected['code_fences'],
            'boundary_blocked':missing_boundary,'finish_reason':finish,
            'alternative_partition_count':len(good)}
