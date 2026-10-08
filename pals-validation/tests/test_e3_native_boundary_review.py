"""Layout exploration only; never equate a numbered list with valid reasoning."""
import unittest

from pals_validation.e3.native_boundary_review import inspect, markers
from pals_validation.e3.greedy_parse_v3 import parse


class NativeLayoutReviewTests(unittest.TestCase):
    def test_existing_multistep_preserved(self):
        raw='<step>1. First fact\n2. Another fact</step><step>Conclusion</step><answer>A</answer>'
        r=inspect(raw,'boundary','mmlu')
        self.assertEqual(r['steps'],parse(raw,'boundary','mmlu')['steps'])
        self.assertEqual(r['candidate_partitions'],[])
        self.assertFalse(r['formal_eligible'])

    def test_single_block_numbered_sequence_keeps_every_character(self):
        raw='<step>Intro text.\n\n1. **Compute A**: result.\n2. **Use A**: conclusion.\nFinal explanation.</step><answer>A</answer>'
        r=inspect(raw,'boundary','gpqa');c=r['candidate_partitions'][0]
        self.assertTrue(r['candidate_with_existing_process_boundary'])
        self.assertFalse(r['v3_scoreable'])
        self.assertEqual(''.join(x['text'] for x in c['steps']),raw[slice(*r['region'])])
        self.assertTrue(c['steps'][0]['text'].startswith('Intro text.'))
        self.assertIn('list_may_enumerate_facts_or_cases',c['cautions'])
        self.assertEqual(len(c['steps']),2)

    def test_missing_answer_not_silently_accepted(self):
        raw='<step>Intro\nStep 1: Compute.\nStep 2: Use it.</step>'
        r=inspect(raw,'eos','mmlu')
        self.assertTrue(r['native_candidates'])
        self.assertTrue(r['boundary_blocked'])
        self.assertFalse(r['candidate_with_existing_process_boundary'])

    def test_restarts_gaps_and_nested_lists(self):
        for sequence in ('1. A\n2. B\n1. C\n2. D','1. A\n3. C'):
            r=inspect('<step>'+sequence+'</step><answer>A</answer>','boundary','gpqa')
            self.assertFalse(r['native_candidates'])
        text='1. A\n    1. nested\n    2. nested\n2. B'
        found,_=markers(text,0,len(text))
        self.assertEqual([m['number'] for m in found],[1,2])

    def test_code_math_and_answer_not_counted(self):
        raw='<step>Text\n```python\n1. Not reasoning\n2. Still code\n```\n\\[\n1. Formula\n2. Formula\n\\]\n</step><answer>\n1. Answer\n2. Answer</answer>'
        r=inspect(raw,'boundary','humaneval')
        self.assertFalse(r['native_candidates'])
        self.assertEqual(r['markers'],[])

    def test_mixed_code_flagged_separately_from_existing_boundary(self):
        raw='<step>1. A\n2. B\n```python\nprint(1)\n```</step><answer>A</answer>'
        r=inspect(raw,'boundary','humaneval')
        self.assertTrue(r['native_candidates'])
        self.assertTrue(r['candidate_with_existing_process_boundary'])
        self.assertFalse(r['candidate_without_code_mixture'])

    def test_markdown_named_steps_and_multiple_heading_levels(self):
        raw='<step>Intro\n### Step 1: A\nresult\n### Step 2: B\nresult</step><answer>A</answer>'
        r=inspect(raw,'boundary','gpqa')
        self.assertEqual(r['candidate_partitions'][0]['kind'],'named_step')
        self.assertTrue(r['native_candidates'])
        raw='<step>## Main\n### Part A\na\n### Part B\nb\n## Conclusion\nx</step><answer>A</answer>'
        r=inspect(raw,'boundary','gpqa')
        self.assertEqual(r['alternative_partition_count'],2)

    def test_no_sentence_splitting_and_truncation_still_blocked(self):
        self.assertFalse(inspect('<step>First. Next. Finally.</step><answer>A</answer>',
                                 'boundary','gpqa')['native_candidates'])
        r=inspect('<step>Step 1: A\nStep 2: unfinished','length','gpqa')
        self.assertTrue(r['native_candidates'])
        self.assertFalse(r['candidate_with_existing_process_boundary'])


if __name__=='__main__':unittest.main()
