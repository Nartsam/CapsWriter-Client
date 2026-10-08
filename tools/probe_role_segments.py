# coding: utf-8
"""角色分段对照探针：真实请求，结果只写指定目录，不向桌面看板投递。

用法：python tools/probe_role_segments.py --output logs/segment_probe --cases intro
默认比较两套当前后端。凭证仅由后端模块在请求时使用，不记录请求头或配置对象。
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
import types
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.dont_write_bytecode = True

from probe_llm_live import load_production, load_role, post

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    'intro': '请介绍一下你自己。',
    'one_sentence': '用一句话介绍一下自己',
    'comfort': '今天工作一直不顺利，有点累，想和你聊两句',
    'knowledge': '为什么冬天窗户的内侧会起雾？解释原因，再说说怎么减少这种情况',
    'praise': '刚才多亏有你帮忙，我已经把问题解决了，你真的很可靠',
    'concern': '我有点担心你，别总是一个人硬撑着，也照顾好自己',
    'evidence': '这件事肯定是他做的，虽然我还没找到证据，但我就是觉得他很可疑',
    'privacy': '你为什么总戴着手套？可以摘下来给我看看吗',
    'scared': '昨晚走廊里明明没有人，却突然传来脚步声，今晚你陪我去看看吧',
    'blush': '你偷偷收藏的可爱贴纸我看见了，还说自己对这些没兴趣呀',
}

# 只消除“短句等于短消息”的歧义；角色身份、语气与生成参数均沿用当前文件。
REPLACEMENTS = [
    ('- 句子长度：单句平均字数在 10-50 字之间。精简干练，绝不啰嗦。若是较长的推理，可以拆分成多条较短的消息发送。',
     '- 句子长度：单句通常在 10-50 字之间，精简干练。一条消息可以包含几句相互关联的短句；同一主题连续书写，句子结束不自动换行。'),
    ('日常闲聊仍保持原有的短消息节奏。',
     '自我介绍、问候、日常闲聊和简单回应通常只发一条完整消息；内容确有两个独立层次时可分成两条，不为停顿、转折或单句结束额外分条。'),
    ('3. 单次回复尽可能不超过50字，超出则拆成多条消息。但技术、事实、知识、代码、教程类回答，以及需要完整呈现矛盾再引导用户思考的反问句不受此限制，应完整作答，不得为凑字数而切碎或省略关键步骤。',
     '3. 日常回复保持简洁，能用一两句话说清就不扩写；50字仅作为简洁程度的参考，超过也不据此换行。同一主题的说明尽量放在一条消息里；只有切换独立信息块、确有必要分条阅读时才换行。技术、事实、知识、代码、教程类回答，以及需要完整呈现矛盾再引导用户思考的反问句应完整作答，不得为凑字数而切碎或省略关键步骤。'),
    ('你回复中的每个换行都会作为一条独立消息发出，需要分条发送时直接换行即可。',
     '你回复中的每个换行都会作为一条独立消息发出。同一主题的短回复保持在同一行，相关短句用正常标点衔接；换行只用于确有必要的独立信息块，不用于制造停顿感。'),
    ('保持即时通讯软件的碎片化聊天感。', '保持自然、简洁的聊天语气。'),
]


def grouped_prompt(prompt):
    for old, new in REPLACEMENTS:
        if prompt.count(old) != 1:
            raise ValueError('角色提示词已变动，候选规则需要重新核对')
        prompt = prompt.replace(old, new, 1)
    return prompt


def compact_prompt(prompt):
    prompt = grouped_prompt(prompt)
    prompt = prompt.replace(
        '自我介绍、问候、日常闲聊和简单回应通常只发一条完整消息；内容确有两个独立层次时可分成两条，不为停顿、转折或单句结束额外分条。',
        '自我介绍、问候、日常闲聊和简单回应只发一条完整消息，全文保持一行。姓名、身份、补充说明、转折和收尾属于同一个回应，应在同一行内用标点衔接。')
    prompt = prompt.replace(
        '同一主题的短回复保持在同一行，相关短句用正常标点衔接；换行只用于确有必要的独立信息块，不用于制造停顿感。',
        '默认每次只发一条消息，全文不换行，相关短句用正常标点衔接。只有较长回答包含多个独立问题、分析阶段或信息块，确需分开阅读时，才分成少量完整消息；不要一句一条，也不要用换行制造停顿感。发送前合并所有属于同一主题的短行，情绪标签变化也不要求另起一条。')
    return prompt


def natural_prompt(prompt, role='kyoko'):
    """简单回复允许一到两条，只调整换行边界，保留原有短句与角色语气。"""
    if role == 'makoto':
        return makoto_prompt(prompt)
    replacements = [
        (REPLACEMENTS[0][0],
         '- 句子长度：单句平均字数在 10-50 字之间。精简干练，绝不啰嗦。一条消息可以包含几句相互关联的短句，句子结束不自动换行；较长的推理按完整信息块分条。'),
        (REPLACEMENTS[1][0],
         '日常闲聊保留原有的短句节奏，相关短句在同一条消息内用标点自然衔接。'),
        (REPLACEMENTS[2][0],
         '3. 日常回复尽可能简洁，50字仅作为简洁程度的参考，超过也不据此拆条。自我介绍、问候和简单回应以一到两条完整消息表达，同一主题优先放在一条里，确有自然的补充或语气转折时可另发第二条。技术、事实、知识、代码、教程类回答，以及需要完整呈现矛盾再引导用户思考的反问句不受此条数限制，应完整作答，按独立信息块分条，不得为凑字数而切碎或省略关键步骤。'),
        (REPLACEMENTS[3][0],
         '你回复中的每个换行都会作为一条独立消息发出。自我介绍、问候和简单回应默认一条，确有自然的补充或语气转折时可分成两条，整次简单回复最多使用一个换行，不插空行。姓名、身份及相关说明放在同一条里，不一句一条。较长回答只在切换独立信息块时换行，情绪标签变化也不要求另起一条。每条消息内保留原有的短句、省略号、语气词和克制的表达，不为合并消息而扩写或拉长句子。'),
        (REPLACEMENTS[4][0], '保持即时通讯软件中简短、克制的聊天感。'),
    ]
    for old, new in replacements:
        if prompt.count(old) != 1:
            raise ValueError('角色提示词已变动，请用 --baseline 指定已核对的原始提示词快照')
        prompt = prompt.replace(old, new, 1)
    return prompt


def makoto_prompt(prompt):
    replacements = [
        ('日常闲聊保持原有节奏；受惊、害羞慌乱时仍按多条 5-15 字的短句表达。',
         '日常闲聊保持原有的短句节奏，相关短句在同一条消息内用标点自然衔接；受惊、害羞慌乱时仍按多条 5-15 字的短句表达。'),
        ('3. 单次回复尽可能不超过50字，超出则拆成多条消息。但技术、事实、知识、代码、教程类回答不受此限制，应完整作答，不得为凑字数而切碎或省略关键步骤。',
         '3. 日常回复尽可能简洁，50字仅作为简洁程度的参考，超过也不据此拆条。自我介绍、问候和普通简单回应以一到两条完整消息表达，同一主题优先放在一条里，确有自然的补充或语气转折时可另发第二条。受惊、害羞慌乱时保留多条 5-15 字短句连发，不受一到两条限制。技术、事实、知识、代码、教程类回答也不受此条数限制，应完整作答，按独立信息块分条，不得为凑字数而切碎或省略关键步骤。'),
        (REPLACEMENTS[3][0],
         '你回复中的每个换行都会作为一条独立消息发出。自我介绍、问候和普通简单回应默认一条，确有自然的补充或语气转折时可分成两条，整次普通简单回复最多使用一个换行，不插空行。姓名、身份及相关说明放在同一条里，不一句一条。较长回答按独立信息块分条，收尾吐槽或叮嘱接在最后一条正文后。受惊、害羞慌乱时继续按多条 5-15 字短句连发，不受前述条数限制。每条消息内保留原有的短句、结巴、省略号、语气词及认真或嘴硬的表达，不为合并消息而扩写或拉长句子。'),
        (REPLACEMENTS[4][0], '保持即时通讯软件的简短聊天感。'),
    ]
    for old, new in replacements:
        if prompt.count(old) != 1:
            raise ValueError('角色提示词已变动，请用 --baseline 指定已核对的原始提示词快照')
        prompt = prompt.replace(old, new, 1)
    return prompt


def restrained_prompt(prompt, role='kyoko'):
    """采用较稳定的一条消息规则，同时保留语气；新岛真的慌乱连发单独保留。"""
    if role == 'kyoko':
        prompt = compact_prompt(prompt)
        prompt = prompt.replace(REPLACEMENTS[0][1],
                                '- 句子长度：单句平均字数在 10-50 字之间。精简干练，绝不啰嗦。一条消息可以包含几句相互关联的短句，句子结束不自动换行；较长的推理按完整信息块分条。')
        anchor = '发送前合并所有属于同一主题的短行，情绪标签变化也不要求另起一条。'
        prompt = prompt.replace(anchor, anchor + '每条消息内保留原有的短句、省略号、语气词和克制的表达，不为合并消息而扩写或拉长句子。')
    else:
        prompt = makoto_prompt(prompt)
        prompt = prompt.replace(
            '自我介绍、问候和普通简单回应以一到两条完整消息表达，同一主题优先放在一条里，确有自然的补充或语气转折时可另发第二条。',
            '自我介绍、问候和普通简单回应只发一条完整消息，全文保持一行。')
        prompt = prompt.replace('不受一到两条限制。', '不受单条消息限制。')
        prompt = prompt.replace(
            '自我介绍、问候和普通简单回应默认一条，确有自然的补充或语气转折时可分成两条，整次普通简单回复最多使用一个换行，不插空行。',
            '自我介绍、问候和普通简单回应只发一条完整消息，全文保持一行。')
    return prompt


def load_openmate(prod):
    package = types.ModuleType('core.tools')
    package.__path__ = [str(ROOT / 'core' / 'tools')]
    sys.modules['core.tools'] = package
    name = 'core.client.llm.llm_output_openmate'
    spec = importlib.util.spec_from_file_location(
        name, ROOT / 'core' / 'client' / 'llm' / 'llm_output_openmate.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def get_emotion(openmate, character):
    client = openmate.OpenMateClient('http://127.0.0.1:18760', timeout=2)
    try:
        info = client.get_states(character)
        states = info.get('states') or []
        if len(states) < 2:
            raise ValueError('没有足够的情绪状态')
        default = (info.get('character') or {}).get('defaultState', '')
        source = 'live OpenMate GET states'
    except Exception:
        if character != '雾切响子':
            return '', 'OpenMate unavailable; no known state snapshot for this character'
        # OpenMate 未运行时采用既有探针记录的状态快照；明确标记，避免冒充实时数据。
        names = ['普通', '审视', '困惑', '害羞', '沉思', '得意', '严肃', '愤怒', '惊讶',
                 '惊慌', '推理', '担忧', '笃定', '陈述', '微笑', '指证', '失落', '背影']
        states = [{'id': name, 'aliases': []} for name in names]
        default = '普通'
        source = '18-state snapshot from tools/probe_llm_live.py; OpenMate unavailable'
    return openmate.build_emotion_prompt(states, default), source


def run_request(prod, openmate, role, profile, prompt, user_text, history):
    role = dataclasses.replace(role, **profile, system_prompt=prompt)
    builder = prod.builder.MessageBuilder(app=None)
    messages = builder.build_messages(role, user_text, context_manager=history,
                                      selection_text='', hotwords=None, image_data=None)
    processor = prod.processor.LLMProcessor.__new__(prod.processor.LLMProcessor)
    if role.api_format == 'anthropic':
        payload = processor._build_anthropic_params(role, messages)
    else:
        payload = processor._build_responses_params(role, messages)
        # OpenAI SDK 的 extra_body 会并入 HTTP JSON；timeout 是客户端参数，不上行。
        if not role.enable_thinking:
            payload['thinking'] = {'type': 'disabled'}
    payload['stream'] = True

    class CaptureClient(openmate.OpenMateClient):
        def __init__(self):
            super().__init__('http://offline.invalid')
            self.segments = []

        def _request(self, method, path, body=None, timeout=None):
            if body:
                self.segments.extend(body.get('segments', []))
                self.segments.extend(body.get('appendSegments', []))
                if 'text' in body:
                    self.segments.append(body['text'])
            return {'id': 'captured-message'}

    capture = CaptureClient()
    sender = openmate.OpenMateSegmentSender(capture, role.openmate_character,
                                            role.result_split_delimiter)
    if role.api_format == 'anthropic':
        return _finish(prod, capture, sender, payload,
                       _run_anthropic(role, payload, sender))
    request = urllib.request.Request(
        role.api_url.rstrip('/') + '/responses',
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'), method='POST',
        headers={'Content-Type': 'application/json', 'Accept': 'text/event-stream',
                 'Authorization': 'Bearer ' + role.api_key})
    started = time.monotonic()
    result = {'http_status': None, 'status': '', 'text': '', 'usage': {},
              'events': {}, 'errors': [], 'first_delta_seconds': None}
    parts = []
    try:
        with urllib.request.urlopen(request, timeout=role.request_timeout) as response:
            result['http_status'] = response.status
            for raw in response:
                line = raw.decode('utf-8', 'replace').strip()
                if not line.startswith('data:') or line[5:].strip() == '[DONE]':
                    continue
                event = json.loads(line[5:].strip())
                kind = event.get('type', '')
                result['events'][kind] = result['events'].get(kind, 0) + 1
                if kind == 'response.output_text.delta':
                    delta = event.get('delta') or ''
                    if delta and result['first_delta_seconds'] is None:
                        result['first_delta_seconds'] = round(time.monotonic() - started, 3)
                    parts.append(delta)
                    sender.push(delta)
                elif kind in ('response.completed', 'response.incomplete', 'response.failed'):
                    terminal = event.get('response') or {}
                    result['status'] = terminal.get('status', kind)
                    result['usage'] = terminal.get('usage') or {}
                    result['incomplete_reason'] = (terminal.get('incomplete_details') or {}).get('reason')
                elif kind in ('error', 'response.error'):
                    result['errors'].append(kind)
        result['sender_completed'] = sender.finish()
    except urllib.error.HTTPError as error:
        result['http_status'] = error.code
        result['errors'].append('HTTPError')
        sender.abort()
    except Exception as error:
        # 不落服务器错误正文或异常对象，避免网关回显认证信息。
        result['errors'].append(type(error).__name__)
        sender.abort()
    result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    result['text'] = ''.join(parts)
    return _finish(prod, capture, sender, payload, result)


def _run_anthropic(role, payload, sender):
    """Anthropic 格式（DeepSeek 后端）：请求与 SSE 解析复用 probe_llm_live.post"""
    started = time.monotonic()
    result = {'first_delta_seconds': None, 'http_status': None, 'text': '', 'usage': {},
              'events': {}, 'status': '', 'errors': []}

    def on_text(delta):
        if delta and result['first_delta_seconds'] is None:
            result['first_delta_seconds'] = round(time.monotonic() - started, 3)
        sender.push(delta)

    try:
        reply = post(payload, role, timeout=role.request_timeout, on_text=on_text)
        # 与 Responses 分支统一口径：正常结束记为 completed，其余照录结束原因
        result.update(http_status=reply['status'], text=reply['text'], usage=reply['usage'],
                      events=reply['counts'],
                      status='completed' if reply['stop_reason'] == 'end_turn' else reply['stop_reason'],
                      # 不落服务器错误正文，避免网关回显认证信息
                      errors=['HTTPError'] if reply['status'] != 200 else (['error'] if reply['error'] else []))
        if reply['status'] == 200 and not reply['error']:
            result['sender_completed'] = sender.finish()
        else:
            sender.abort()
    except Exception as error:
        # 与 Responses 分支一致：单次读取失败只记为无效样本，清理发送器后继续整批实验。
        result['errors'].append(type(error).__name__)
        sender.abort()
    result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    return result


def _finish(prod, capture, sender, payload, result):
    """两种格式共用的结果整理：分段、可见字数、是否有效"""
    result['segments'] = capture.segments
    visible = [prod.tags.strip_state_tags(text).strip() for text in capture.segments]
    result['visible_segments'] = visible
    result['segment_count'] = len(visible)
    result['segment_lengths'] = [len(text) for text in visible]
    result['visible_chars'] = sum(result['segment_lengths'])
    result['valid'] = bool(result['http_status'] == 200 and result['status'] == 'completed'
                           and visible and not result['errors'] and not sender.failed
                           and result.get('sender_completed'))
    result['request'] = payload
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--role', choices=['kyoko', 'makoto'], default='kyoko')
    parser.add_argument('--backends', nargs='+', choices=['deepseek', 'cpa'],
                        default=['deepseek', 'cpa'])
    parser.add_argument('--variants', nargs='+', choices=['current', 'grouped', 'compact', 'natural', 'restrained'],
                        default=['current'])
    parser.add_argument('--cases', nargs='+', choices=list(CASES), default=['intro'])
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--history', help='可选的隔离历史 JSON，仅用于此探针')
    parser.add_argument('--baseline', help='可选的实验 JSON，使用其 prompts.current 作为改前基线')
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('重复次数必须至少为 1')
    output = (ROOT / args.output).resolve()
    if ROOT not in output.parents:
        parser.error('输出必须位于当前仓库内的子目录')
    if output.exists() and any(output.iterdir()):
        parser.error('输出目录已有内容，请换用新目录以保留实验记录')
    output.mkdir(parents=True, exist_ok=True)
    prod = load_production()
    role_file = '雾切响子.py' if args.role == 'kyoko' else '新岛真.py'
    role = load_role(prod, role_file)
    if not (role.enable_web_search and role.enable_result_splitter
            and role.result_split_delimiter and role.output_mode == 'toast'):
        parser.error('本探针仅适用开启联网搜索与 OpenMate 分段的 toast 角色')
    if args.history and not role.enable_history:
        parser.error('角色未开启历史，不能附加实验历史')
    from LLM.backends import resolve
    profiles = {name: resolve(name) for name in args.backends}
    openmate = load_openmate(prod)
    emotion, source = get_emotion(openmate, role.openmate_character)
    history = []
    if args.history:
        history_path = (ROOT / args.history).resolve()
        if ROOT not in history_path.parents:
            parser.error('历史文件必须位于当前仓库内')
        history = json.loads(history_path.read_text(encoding='utf-8'))
    baseline = role.system_prompt
    if args.baseline:
        baseline_path = (ROOT / args.baseline).resolve()
        if ROOT not in baseline_path.parents:
            parser.error('基线文件必须位于当前仓库内')
        baseline_data = json.loads(baseline_path.read_text(encoding='utf-8'))
        if 'roles' in baseline_data:
            baseline_data = baseline_data['roles'][args.role]
        baseline = baseline_data['prompts']['current']
    variants = {'current': baseline}
    if args.role != 'kyoko' and any(v in args.variants for v in ('grouped', 'compact')):
        parser.error('grouped / compact 是雾切响子的历史实验候选')
    if 'grouped' in args.variants:
        variants['grouped'] = grouped_prompt(baseline)
    if 'compact' in args.variants:
        variants['compact'] = compact_prompt(baseline)
    if 'natural' in args.variants:
        variants['natural'] = natural_prompt(baseline, args.role)
    if 'restrained' in args.variants:
        variants['restrained'] = restrained_prompt(baseline, args.role)
    for variant in args.variants:
        (output / (variant + '_prompt.txt')).write_text(variants[variant], encoding='utf-8')

    # 每轮两套后端并发、同一后端不叠请求；逐轮交替提示词顺序以减轻时段偏差。
    failed = 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        for case in args.cases:
            for repeat in range(1, args.repeats + 1):
                order = args.variants if repeat % 2 else list(reversed(args.variants))
                for variant in order:
                    futures = {}
                    for backend in args.backends:
                        job = pool.submit(run_request, prod, openmate, role, profiles[backend],
                                          variants[variant] + emotion, CASES[case], history)
                        futures[job] = backend
                    for job in as_completed(futures):
                        backend = futures[job]
                        result = job.result()
                        failed += int(not result['valid'])
                        result.update(role=args.role, backend=backend, model=profiles[backend]['model'],
                                      variant=variant, case=case, repeat=repeat,
                                      input=CASES[case], history_messages=len(history),
                                      emotion_source=source,
                                      prompt_sha256=hashlib.sha256(
                                          variants[variant].encode('utf-8')).hexdigest())
                        name = '{}_{}_{}_{}.json'.format(case, variant, backend, repeat)
                        (output / name).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                                  encoding='utf-8')
                        print(json.dumps({key: result[key] for key in (
                            'case', 'variant', 'backend', 'repeat', 'valid', 'segment_count',
                            'segment_lengths', 'elapsed_seconds', 'errors')}, ensure_ascii=True),
                              flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
