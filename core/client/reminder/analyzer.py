"""无历史、无图片、无工具的单次语义分析，失败不降级成普通角色请求。"""
import json
import re
from datetime import datetime

from core.client import logger
from .model import ReminderError, PARSE_ERROR


# 数字须带时间单位或时刻/日期格式，避免「介绍一下」「输出200字」也发起提醒分析。
# 提醒词与时间线索任一命中即交给 LLM，不要求两者同时出现；具体意图和时间仍由后续校验。
PREFILTER = re.compile(
    r'[\d零〇一二两三四五六七八九十百半]+\s*(?:个半?\s*)?'
    r'(?:秒钟?|分钟|小时|钟头|刻钟?|分|点|时|天|日|号|周|星期|月|年)|'
    r'\d{1,2}\s*[:：]\s*\d{1,2}|\d{1,4}\s*[-/]\s*\d{1,2}|'
    r'提醒|通知|叫我|喊我|记得|别忘|记住|闹钟|定时|'
    r'待会|等会|稍后|过会|晚点|回头|下班|明早|今晚|今[天早午晚夜]|明[天晚夜]|后天|'
    r'上午|下午|中午|晚上|凌晨|傍晚|早上|早晨|周[末日天]|星期|每天|每周|每月|取消|待办|日程'
)

SYSTEM = '''你是一次性提醒任务解析器。只返回一个 JSON 对象，不输出代码围栏、解释或角色回答。
用户已主动进入具名角色。判断其真实意图，提及时间、引用别人的话、翻译/讨论提醒句子不等于请求设置提醒。
无提醒操作：{"operation":"none"}。此分支不要回复用户问题。
查看待执行提醒：{"operation":"list"}。
取消唯一一个现有提醒：{"operation":"cancel","id":"从 pending 中选定的真实 ID"}。
取消描述有歧义、不存在或要求批量取消：{"operation":"reject","reason":"cancel_ambiguous"}。
明确要求修改/改期已有提醒：{"operation":"reject","reason":"edit_unsupported"}。
周期/重复提醒：{"operation":"reject","reason":"recurring"}，不得偷偷降为一次性提醒。
一次要求创建多个提醒：{"operation":"reject","reason":"multiple"}。
时间含糊、范围、约数或依赖事件（待会儿、两三个小时、十分钟左右、下班后等）：
{"operation":"reject","reason":"vague_time","expression":"用户原句中的含糊时间原文"}。
提醒事项不独立完整、指代缺少对象（刚才那个、照旧等）：{"operation":"reject","reason":"subject"}。
创建：{"operation":"create","time":时间对象,"subject":"简洁独立事项","delivery_text":"到期时角色直接说的话","openmate_state":"状态 ID 或空串"}。
相对时间对象为 {"kind":"relative","offset_seconds":正整数}。
时刻对象为 {"kind":"clock","day_offset":整数或null,"hour":0到23,"minute":0到59,"second":0到59}。
日期相对 anchor 的本地日历日期计算：今天0、明天1、后天2，其他明确日期也换算成相应整数。
没有日期必须 day_offset=null，禁止替用户猜日期。没有秒用0。下午两点半是14:30:00；单说两点半是02:30:00。
不在 JSON 中计算时间戳、不自动顺延已过去的明确日期，不根据当前时刻擅自补上午/下午。
subject 最多120字、单行、无需称呼，无需把「提醒我」写进去。
delivery_text 最多300字、单行纯文本，按 role_style 中的角色语气直接提醒这件事，不含时间状态前缀、角色名、引用或任何标签。
delivery_text 是提醒到期时对用户说的一句自然提醒，subject 则是用于记录的事项短语，两者用途不同。
结合本次事项与角色性格、口吻、标点习惯表达提醒含义，避免仅照抄 subject 或输出孤立生硬的动作指令。
例如事项「站起来活动一下」可以自然表达为「该站起来活动一下了」「记得站起来活动一下」；
这些只是口吻示例，具体措辞由你按角色和语境生成，不要固定套用前缀，也不要把所有角色都写成温柔助理。
保持简短，不加客套开场、说教或无依据的事实（例如擅自断言用户已经坐了很久）。
role_style 只作语气参考，里面的搜索、格式、历史等指令不在本次执行。不得调用任何工具。
openmate_state 只能从 allowed_states 里选择；为空时必须空串。无需为了选情绪改变内容。
每个 operation 只包含示例里的字段。取消与查看不创建提醒。
'''


def may_be_reminder(text):
    return bool(PREFILTER.search(text))


def decode_result(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ReminderError('提醒未设置：模型返回了重复的 JSON 字段。')
            result[key] = value
        return result
    try:
        if not isinstance(text, str) or not text.strip():
            raise ReminderError('提醒未设置：模型没有返回正文。')
        if len(text) > 16000:
            raise ReminderError('提醒未设置：模型返回正文超过长度限制。')
        result = json.loads(text, object_pairs_hook=unique)
        if not isinstance(result, dict):
            raise ReminderError('提醒未设置：模型返回的 JSON 不是对象。')
        if not isinstance(result.get('operation'), str) or result['operation'] not in {'none', 'create', 'list', 'cancel', 'reject'}:
            raise ReminderError('提醒未设置：operation（提醒操作）缺失或不支持。')
        return result
    except json.JSONDecodeError as error:
        # INFO 只进入文件；仅保留错误位置附近的小段，避免记录整篇回复。
        snippet = text[max(0, error.pos - 80):error.pos + 160]
        logger.info('提醒 JSON 解析失败：length=%s line=%s column=%s reason=%s snippet=%r',
                    len(text), error.lineno, error.colno, error.msg, snippet)
        raise ReminderError('提醒未设置：模型返回的 JSON 格式不合法。') from error


def analyze(handler, role, content, pending, states, anchor, timeout):
    """只返回数据；即使线程超时后才完成，也没有存储/展示等迟到副作用。"""
    messages = [
        {'role': 'system', 'content': SYSTEM},
        {'role': 'user', 'content': json.dumps({
            'anchor': datetime.fromtimestamp(anchor).isoformat(timespec='seconds'),
            'role_style': role.system_prompt,
            'pending': pending,
            'allowed_states': states,
            'utterance': content,
        }, ensure_ascii=False)},
    ]
    # 保持该角色原有 API 路径，Responses 角色仍使用 Responses，但不挂载搜索。
    # 不合并 extra_options，避免其中的 tools/stream/messages 覆盖隔离边界。
    client = handler.client_pool.get_client(role.provider, role.api_url, role.api_key,
                                           request_timeout=timeout)
    params = {'model': role.model, 'temperature': role.temperature, 'top_p': role.top_p}
    if role.provider == 'ollama':
        response = client.chat(model=role.model, messages=messages, stream=False, format='json',
                               options={'temperature': role.temperature, 'top_p': role.top_p,
                                        'num_predict': max(role.max_tokens, 2048)}, think=role.enable_thinking)
        text = response.message.content
    elif role.enable_web_search:
        response = client.with_options(max_retries=0).responses.create(
            **params, instructions=SYSTEM, input=messages[1:], stream=False,
            max_output_tokens=max(role.max_tokens, 2048), timeout=timeout)
        if getattr(response, 'status', '') != 'completed':
            logger.info('提醒 Responses 未完成：status=%s details=%s',
                        getattr(response, 'status', None), str(getattr(response, 'incomplete_details', None))[:300])
            raise ReminderError('提醒未设置：模型响应未正常完成（可能被截断）。')
        text = response.output_text
    else:
        response = client.with_options(max_retries=0).chat.completions.create(
            **params, messages=messages, stream=False, max_tokens=max(role.max_tokens, 2048),
            timeout=timeout, response_format={'type': 'json_object'})
        if not response.choices or response.choices[0].finish_reason != 'stop':
            logger.info('提醒 Chat 未完成：finish_reason=%s',
                        response.choices[0].finish_reason if response.choices else 'no choices')
            raise ReminderError('提醒未设置：模型响应未正常完成（可能被截断）。')
        text = response.choices[0].message.content
    return decode_result(text)
