"""One server-owned routing vocabulary. Classification never grants execution rights."""
from typing import Literal, get_args
from types import MappingProxyType
import re

VERSION = 'banking-tools-v1'
IntentKind = Literal['balance','transactions','bills','card','subscriptions','wealth','transfer_draft','plan_draft','statement_import','fx_budget','entity_lookup','public_notices','clarification','unsupported']
TRANSFER_STEPUP_FEN = 100_000


def entry(title, view=None, section=None, effect='navigate', next_step='查看页面后另行确认具体操作'):
    return {'title':title,'route':{'view':view,'section':section} if view else None,'effect':effect,'next_step':next_step}


INTENTS = MappingProxyType({
    'balance':entry('可用余额',effect='read',next_step='只读结果，无资金授权'),
    'transactions':entry('交易记录','activity',effect='read',next_step='只读结果，无资金授权'),
    'bills':entry('账单洞察','bills'),
    'card':entry('卡片管理','cards'),
    'subscriptions':entry('订阅与代扣','subscriptions',next_step='读取订阅明细需要明确确认；取消需单独授权'),
    'wealth':entry('理财实验室','wealth',next_step='先核对适当性和产品详情；申购、赎回需强验证'),
    'transfer_draft':entry('转账草案',effect='draft',next_step='核对交易预览；按累计金额和风险决定确认或强验证'),
    'plan_draft':entry('定时预留与提醒','home',effect='draft',next_step='核对时间、预算和订阅，再确认启用'),
    'statement_import':entry('账单导入','evidence','imports',next_step='文件解析后核对预览，再确认保存'),
    'fx_budget':entry('跨境预算','evidence','fx',next_step='先试算；资金预留须另行确认，不执行换汇'),
    'entity_lookup':entry('机构登记','evidence','entities',next_step='确认公开 LEI 后查询，不证明账户归属'),
    'public_notices':entry('官方风险通知','evidence','notices',effect='read',next_step='公开通知不是账户风险判定'),
    'clarification':entry('需要澄清',effect='none',next_step='等待补充信息，不执行业务'),
    'unsupported':entry('当前未支持',effect='none',next_step='不执行业务'),
})
assert set(get_args(IntentKind)) == set(INTENTS)

RULE_TERMS = ('机构登记','机构身份','LEI','导入账单','上传账单','跨境预算','汇率','防骗通知','风险通知','官方提醒','最新骗案','余额','还剩','账户有多少','流水','交易记录','最近交易','账单','消费','重复扣费','卡片','银行卡','挂失','冻结','订阅','续费','代扣','转账','转给','汇款','打给','预留','留出','计划','安排','理财','申购','基金','风险测评','投资产品')


def transfer_level(spent, amount, risk_tier):
    return 'red' if spent + amount > TRANSFER_STEPUP_FEN or risk_tier == 'review' else 'yellow'


def needs_scope_clarification(text):
    # Deliberately conservative: do not silently discard an additional domain or condition.
    domains = [r'余额|还剩|账户有多少',r'账单|消费|重复扣费',r'转账|转给|汇款|打给|给.{1,20}转',r'理财|申购|基金|赎回',r'卡片|银行卡|挂失|冻结',r'汇率|跨境预算']
    count = sum(bool(re.search(pattern,text)) for pattern in domains)
    condition = re.search(r'如果|要是|否则|足够|余额够|不影响|前提|等.+再',text)
    return count > 1 or bool(condition and re.search(r'转账|转给|给.{1,20}转|申购|赎回',text))


def decorate(result):
    definition = INTENTS[result['kind']]
    return {**result,'contract_version':VERSION,'capability':dict(definition),'funds_authorized':False}


def catalogue():
    return {'version':VERSION,'interpretation_authorizes_funds':False,'intents':[{'kind':kind,**value} for kind,value in INTENTS.items()],
            'transfer_stepup':{'daily_threshold_fen':TRANSFER_STEPUP_FEN,'risk_can_escalate':True,'policy_authority':'server'},
            'scope':'目录描述可用能力；操作确认、设备验证及执行状态始终由业务端点重新校验。'}
