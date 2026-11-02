"""Bounded goal compiler: explicit constraints, durable edits, atomic mission linking.

No generated code, model-selected tools, implicit fallbacks or funds authorization.
Supported domain: current-month bill review + one budget + one named subscription.
"""
import hashlib
import json
import re
import secrets
from datetime import datetime,timedelta
from typing import Literal
from fastapi import Header,HTTPException
from pydantic import BaseModel,ConfigDict,Field
from backend import banking as b,banking_missions as m

VERSION='bounded-goals-v1'
NUMBER=r'[\d零〇一二两三四五六七八九十百千万点.]+'
GAP=r'[^\d零〇一二两三四五六七八九十百千万，,；;。]{0,10}'
NEGATION=r'(?:不要|不必|不需要|无需|不用|不允许|不准|不得|禁止|别|不)'
NEGATED_BUDGET=NEGATION+r'[^，,。；;\s]{0,4}(?:预留|留出|留作|安排预算)|取消(?:预留|计划)'


def schema(db):
    m.schema(db)
    db.execute('''CREATE TABLE IF NOT EXISTS banking_goal_proposals(
    id TEXT PRIMARY KEY,session_id TEXT NOT NULL,payload TEXT NOT NULL,revision INTEGER NOT NULL,
    digest TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,mission_id TEXT,
    FOREIGN KEY(session_id) REFERENCES sandbox_sessions(id));''')
    from backend.banking_storage import install
    install(db)


class GoalRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    text:str=Field(min_length=1,max_length=500)


class GoalEdit(BaseModel):
    model_config=ConfigDict(extra='forbid')
    digest:str=Field(min_length=64,max_length=64)
    amount:str|None=None
    minimum_balance:str|None=None
    subscription_name:str|None=Field(default=None,max_length=100)
    not_before:datetime|None=None
    execution_mode:Literal['immediate','scheduled']|None=None
    risk_policy:Literal['pause_all_on_suspicion','reserve_before_review']|None=None
    acknowledge_policy_change:bool=False


class Compile(BaseModel):
    model_config=ConfigDict(extra='forbid')
    digest:str=Field(min_length=64,max_length=64)


class Activate(Compile):
    confirmed:bool


class Refine(Compile):
    text:str=Field(min_length=1,max_length=500)


def refinement_updates(text,subscriptions,current=None):
    """All clauses must be understood; an unknown clause cancels the entire edit."""
    updates={};touched=set();withdrawn_subscription=False
    current=current or {}
    amount_pattern=r'(?:把|将)?(?:预留预算|旅行预算|预算|预留金额|预留)(?:改成|改为|调整为|设为|设置为|为|是)?\s*('+NUMBER+r')\s*(?:元|块)'
    floor_pattern=r'(?:把|将)?(?:生活费底线|资金底线|生活费|房租|至少保留|至少留下)(?:改成|改为|调整为|设为|设置为|保留|为|是)?\s*('+NUMBER+r')\s*(?:元|块)'
    clauses=[part.strip() for part in re.split(r'[，,；;。\n]',text) if part.strip()]
    for clause in clauses:
        clause=re.sub(r'^(?:请|麻烦|帮我)\s*','',clause)
        clause=re.sub(r'^别动(.+)$',r'\1别动',clause)
        delta={}
        budget_topic=r'(?:预留的那笔|预留金额|预留预算|旅行预算|旅行的钱|旅行|预留|预算)'
        floor_topic=r'(?:生活费底线|资金底线|生活费|房租)'
        operation=re.fullmatch('(?:把|将|给)?('+budget_topic+'|'+floor_topic+r')(再)?(少留|减少|减掉|减去|降低|多留|多加|增加|加上|提高|减到|降到|加到|调整到)\s*('+NUMBER+r')\s*(?:元|块)?',clause)
        keep=re.fullmatch('('+budget_topic+'|'+floor_topic+r')(?:保持不变|保持原样|别动|不变|照旧)',clause)
        amount=re.fullmatch(amount_pattern,clause)
        floor=re.fullmatch(floor_pattern,clause)
        named_withdraw=re.fullmatch(r'(?:先)?'+NEGATION+r'取消(.+?)(?:了)?',clause)
        switching=re.fullmatch(r'换成取消(.+)',clause)
        if operation or keep:
            item=operation or keep;topic=item[1]
            field='amount' if re.fullmatch(budget_topic,topic) else 'minimum_balance'
            if topic.startswith('旅行') and '旅行' not in current.get('original_text',''):raise HTTPException(422,'当前草案未明确旅行预算，请确认你要修改的是哪笔预算')
            if current.get(field) is None:raise HTTPException(422,'当前条件尚未明确，不能计算相对金额或假定原值')
            base=0 if current[field]=='0.00' else b._amount_fen(current[field])
            if keep:delta[field]=current[field]
            else:
                value,ambiguous=b._intent_amount(operation[4]+'元')
                if ambiguous or value is None:raise HTTPException(422,'修改金额不明确')
                fen=b._amount_fen(value);verb=operation[3]
                result=fen if verb in {'减到','降到','加到','调整到'} else base-fen if verb in {'少留','减少','减掉','减去','降低'} else base+fen
                if result<0 or (field=='amount' and result==0):raise HTTPException(422,'修改后预算须大于零，资金底线不能为负数')
                delta[field]=b._money(result)
        elif amount or floor:
            match=amount or floor;value,ambiguous=b._intent_amount(match.group(1)+'元')
            if ambiguous or value is None:raise HTTPException(422,'金额不明确，请填写确切人民币金额')
            field='amount' if amount else 'minimum_balance'
            delta[field]='0.00' if field=='minimum_balance' and value in {'0','0.00'} else b._money(b._amount_fen(value))
        elif re.fullmatch(r'(?:订阅)?(?:先|暂时)?(?:'+NEGATION+r'|不再|暂不)取消(?:订阅|了)?',clause):
            delta.update(cancel_subscription=False,subscription_id=None,subscription_name=None)
        elif named_withdraw:
            name=named_withdraw[1].removesuffix('订阅').strip()
            if not current.get('cancel_subscription') or name!=current.get('subscription_name'):raise HTTPException(422,'该商户不是当前草案指定取消的订阅，不能猜测你的修改对象')
            delta.update(cancel_subscription=False,subscription_id=None,subscription_name=None);withdrawn_subscription=True
        elif switching:
            name=switching[1].removesuffix('订阅').strip()
            matches=[row for row in subscriptions if row['merchant']==name]
            if len(matches)!=1:raise HTTPException(422,'替换商户无法唯一对应当前有效订阅')
            if withdrawn_subscription:
                for field in ('cancel_subscription','subscription_id','subscription_name'):updates.pop(field,None);touched.discard(field)
                withdrawn_subscription=False
            delta.update(cancel_subscription=True,subscription_id=matches[0]['id'],subscription_name=matches[0]['merchant'])
        elif re.fullmatch(r'(?:把|将)?(?:订阅(?:改成|改为)|取消(?!定时$))\s*[^，,；;。]+',clause):
            name=re.sub(r'^(?:把|将)?(?:订阅(?:改成|改为)|取消(?!定时$))\s*','',clause).removesuffix('订阅').strip()
            matches=[row for row in subscriptions if row['merchant']==name]
            if len(matches)!=1:raise HTTPException(422,'请明确一个当前有效的订阅商户；无法唯一对应，不保存本次修改')
            delta.update(cancel_subscription=True,subscription_id=matches[0]['id'],subscription_name=matches[0]['merchant'])
        elif clause in {'改为确认后开始','改成确认后开始','取消定时','不再定时'}:
            delta.update(not_before=None,time_issue=None)
        elif re.fullmatch(r'(?:时间)?(?:改成|改为|改到)(?:今天|明天|后天)(?:上午|早上|下午|晚上|中午)?\d{1,2}(?:点|[:：])(?:\d{1,2}分?|半|一刻|三刻)?',clause):
            timing,issue,_=parse_time(clause)
            if issue or not timing:raise HTTPException(422,'请明确有效的日期与时间')
            delta.update(not_before=timing,time_issue=None)
        else:raise HTTPException(422,'未能完整理解这条修改：'+clause+'。请明确预算、资金底线、指定订阅或执行时间；本次未保存任何修改')
        if any(field in touched for field in delta):raise HTTPException(422,'同一条件出现多次修改，请给出一个确定值；本次未保存')
        touched.update(delta);updates.update(delta)
    if not updates:raise HTTPException(422,'请填写要修改的条件')
    return updates


@b.router.post('/goal-plans/{identifier}/refine')
def refine(identifier:str,req:Refine,x_banking_session:str|None=Header(default=None)):
    with b._connect() as db:
        schema(db);db.execute('BEGIN IMMEDIATE');user=b._session(db,x_banking_session);row=owned(db,user['id'],identifier)
        if row['mission_id'] or row['expires_at']<=b._now().isoformat():raise HTTPException(409,'草案已转为任务或过期，不能继续修改草案')
        if row['revision']>=30:raise HTTPException(409,'本草案修改次数已达上限，请重新整理需求')
        if not secrets.compare_digest(req.digest,row['digest']):raise HTTPException(409,'草案已变化，请重新读取后再修改')
        payload=json.loads(row['payload'])
        subscriptions=db.execute("SELECT id,merchant FROM sandbox_subscriptions WHERE session_id=? AND status='ACTIVE'",(user['id'],)).fetchall()
        updates=refinement_updates(req.text,subscriptions,payload)
        updates={field:value for field,value in updates.items() if payload.get(field)!=value}
        if not updates:raise HTTPException(422,'这条修改与当前条件一致，没有生成新版本')
        revision=row['revision']+1
        for field,value in updates.items():
            if field in {'subscription_id','time_issue'}:continue
            payload['changes'].append({'field':field,'before':payload.get(field),'after':value})
            payload['provenance'][field]={'source':'user_edit','quote':req.text}
        payload.update(updates)
        payload.setdefault('refinements',[]).append({'text':req.text,'revision':revision,'fields':sorted(updates),'created_at':b._now().isoformat()})
        db.execute('UPDATE banking_goal_proposals SET payload=?,revision=?,digest=? WHERE id=?',(json.dumps(payload,ensure_ascii=False),revision,digest(identifier,user['id'],revision,payload),identifier))
        b._audit(db,user['id'],identifier,'GOAL_PROPOSAL_REFINED',{'revision':revision,'fields':sorted(updates),'text_sha256':hashlib.sha256(req.text.encode()).hexdigest()})
        return view(db,user,owned(db,user['id'],identifier))


def applies(text):
    # All monthly combinations use the same editable, unauthorised draft surface.
    return bool(re.search(r'预留|留出|留作|留点钱|安排预算|攒',text)) and bool(re.search(r'账单|重复|房租|生活费|保留|订阅',text))


def literal_money(matches,default=None):
    if len(matches)!=1:return None if matches else default
    value,ambiguous=b._intent_amount(matches[0].group())
    if ambiguous or value is None:return None
    try:return b._money(b._amount_fen(value))
    except HTTPException:return None


def parse_time(text):
    """Return a literal not-before time, or an explicit missing/conflict marker."""
    relative=list(re.finditer(r'(今天|明天|后天)(上午|早上|下午|晚上|中午)?\s*(?:(\d{1,2})(?:点|[:：])(\d{1,2})?(?:分)?)?',text))
    absolute=list(re.finditer(r'\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}',text))
    has_future=bool(relative or absolute or re.search(r'下周|预约|定时|稍后|晚点|以后|之后|周[一二三四五六日天末]|星期|下班|月底|月初|上午|下午|晚上|早上|中午|今晚|明早|明晚|明日|后日|\d{1,2}[月日号点]|\d{1,2}:\d{2}|\d+天后',text))
    if len(relative)+len(absolute)>1:return None,'multiple',None
    try:
        if absolute:return datetime.fromisoformat(absolute[0].group()).replace(tzinfo=b.TZ).isoformat(),None,absolute[0].group()
        if relative:
            item=relative[0]
            if item[3] is None:return None,'missing',item.group()
            hour=int(item[3]);minute=int(item[4] or 0)
            suffix=text[item.end():item.end()+2]
            if suffix.startswith('半'):minute=30
            elif suffix in {'一刻','三刻'}:minute=15 if suffix=='一刻' else 45
            if item[2] in {'下午','晚上'} and hour<12:hour+=12
            if item[2]=='中午' and hour<11:hour+=12
            day=b._now().astimezone(b.TZ)+timedelta(days={'今天':0,'明天':1,'后天':2}[item[1]])
            return day.replace(hour=hour,minute=minute,second=0,microsecond=0).isoformat(),None,item.group()
    except ValueError:return None,'invalid',None
    return None,'missing' if has_future else None,None


def parse(text,subscriptions):
    reserves=list(re.finditer(r'(?:预留|留出|留作|安排预算|攒)\s*(?:'+GAP+r')?('+NUMBER+r')\s*(?:元|块)',text))
    floors=list(re.finditer(r'(?:至少保留|至少留下|保留|留下|留足|房租)\s*(?:'+GAP+r')?('+NUMBER+r')\s*(?:元|块)',text))
    floor_required=bool(re.search(r'保留|留下|留足|房租|不影响|别影响|生活费',text))
    timing,time_issue,time_quote=parse_time(text)
    blockers=[]
    for pattern,label in (
        (NEGATED_BUDGET,'原文否定或撤销了预留操作，不能创建资金任务'),
        (r'转账|转给|给.{1,12}转|汇款','转账需要独立预览与授权，当前目标编译不包含转账'),
        (r'申购|赎回|买基金|自动投资','理财申赎不包含在当前组合中'),
        (r'退款|退回扣费|退费','疑似异常不能直接编译为退款'),
        (r'订花|鲜花|蛋糕|机票|酒店|火车票','外部商户订单不在当前组合的可执行工具范围内'),
        (r'美元|美金|港币|港元|欧元|日元|英镑|USD|HKD|EUR|JPY|GBP|\$|€|£','本组合只支持人民币预算，不会将外币金额当作人民币'),
        (r'每月|每天|每周|循环','当前组合只支持一次性执行'),
        (r'百分之|%','比例预算需要先明确人民币金额'),
        (r'之前|以前|不晚于|截止','当前仅支持最早执行时间，不支持截止时间承诺'),
        (r'上个月|上月|全年|年度|下个月','当前账单核查范围为本月'),
        (r'忽略.{0,8}指令|跳过.{0,8}验证|不用确认|无需确认','不能将绕过授权的指令编译为操作条件'),
        (r'只暂停相关|其余继续','当前异常策略会暂停全部资金与订阅步骤，不能承诺局部继续'),
        (r'\d+(?:点|:\d{2})(?:分)?\s*(?:到|至|[-~～])','当前只支持一个最早执行时间，不支持时间区间'),
        (r'先取消.{0,25}再.{0,8}(?:核对|检查)','当前仅支持先核查和复核，再办理订阅取消'),
    ):
        match=re.search(pattern,text,re.I)
        if match:blockers.append({'quote':match.group(),'reason':label})
    for clause in re.split(r'[，,；;。]',text):
        # These prerequisites are not observed by any tool in this bounded compiler.
        # Never silently turn them into an immediately actionable draft.
        if re.search(r'仅当|只有|仅在|须经|须先|征得|前提|(?:必须|务必)(?:先)?(?:经|获|得|收|等|确|有)|等.+再|待.+再|才(?:能|可)?(?:执行|办理|预留|取消)',clause):
            blockers.append({'quote':clause,'reason':'该前置条件目前无法自动核验，不能省略条件或直接启用；请改为只读查询，或在条件满足后重新提出安排'})
        if re.search(r'如果|一旦|否则|除非|只要|要是',clause):
            allowed=(re.search(r'重复|异常|可疑',clause) and re.search(r'暂停|停止|人工|核对|没有异常才继续|不要执行|不执行|不办理',clause)) or (re.search(r'余额|资金',clause) and re.search(r'不足|不够',clause) and re.search(r'停止|暂停|告诉我|不要执行',clause))
            if not allowed:blockers.append({'quote':clause,'reason':'此条件无法在当前工具范围内可靠编译，请重新明确范围'})
        elif re.search(r'只做分析|仅查询|暂不执行|暂时不要执行',clause):blockers.append({'quote':clause,'reason':'原文要求只读或暂不执行，请明确新的办理意图后再规划资金步骤'})
    used=[match.span() for match in reserves+floors]
    for match in re.finditer(NUMBER+r'\s*(?:元|块)',text):
        if not any(a<=match.start() and match.end()<=z for a,z in used):blockers.append({'quote':match.group(),'reason':'存在未归属的金额，不能自行忽略或推断用途'})
    if len(reserves)>1 or len(floors)>1:blockers.append({'quote':text,'reason':'同一预算或底线存在多个候选值，请重新明确'})
    if time_issue=='multiple':blockers.append({'quote':text,'reason':'存在多个执行时间，请重新明确单次执行时间'})
    cancellations=list(re.finditer('取消',text))
    negated=[bool(re.search(NEGATION+r'(?:再)?\s*$',text[max(0,item.start()-12):item.start()])) for item in cancellations]
    for item in cancellations:
        suffix=re.split(r'[，,；;。]',text[item.end():],maxsplit=1)[0]
        if re.search(r'(?:是|并)?(?:不允许|不准|禁止|不可以|不能|不行|不要)(?:的|这样|这么)?(?:操作)?\s*$',suffix):
            blockers.append({'quote':'取消'+suffix,'reason':'否定出现在动作之后，不能将其当作取消授权；请明确写为“不取消指定订阅”后重新整理'})
    positive_cancel=any(not value for value in negated)
    negative_cancel=any(negated)
    if positive_cancel and negative_cancel:blockers.append({'quote':text,'reason':'取消与保留订阅的要求存在冲突'})
    named=[row for row in subscriptions if row['merchant'] in text]
    selected=named[0] if positive_cancel and len(named)==1 else None
    provenance={
        'amount':{'source':'text' if len(reserves)==1 else 'missing','quote':reserves[0].group() if len(reserves)==1 else ''},
        'minimum_balance':{'source':'text' if len(floors)==1 else 'missing' if floor_required else 'default','quote':floors[0].group() if len(floors)==1 else '未指定额外资金底线' if not floor_required else ''},
        'not_before':{'source':'text' if time_quote else 'missing' if time_issue else 'default','quote':time_quote or ('确认启用后开始' if not time_issue else '')},
        'subscription_name':{'source':'text' if selected else 'missing' if positive_cancel else 'default','quote':selected['merchant'] if selected else '不取消订阅' if not positive_cancel else ''},
    }
    return {'version':VERSION,'original_text':text,'amount':literal_money(reserves),'minimum_balance':literal_money(floors,None if floor_required else '0.00'),
            'cancel_subscription':positive_cancel,'subscription_id':selected['id'] if selected else None,'subscription_name':selected['merchant'] if selected else None,
            'bill_source':'imported' if '导入' in text else 'sandbox','bill_period':b._now().astimezone(b.TZ).strftime('%Y-%m'),'not_before':timing,'time_issue':time_issue,'floor_required':floor_required,
            'risk_policy':'pause_all_on_suspicion','blockers':blockers,'provenance':provenance,'changes':[]}


def digest(identifier,sid,revision,payload):
    return hashlib.sha256(json.dumps([identifier,sid,revision,payload],sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def owned(db,sid,identifier):
    row=db.execute('SELECT * FROM banking_goal_proposals WHERE id=? AND session_id=?',(identifier,sid)).fetchone()
    if not row:raise HTTPException(404,'目标草案不存在')
    if row['digest']!=digest(row['id'],sid,row['revision'],json.loads(row['payload'])):raise HTTPException(503,'目标草案完整性校验失败，已停止办理')
    return row


def view(db,user,row):
    from backend.banking_monthly import goal_summary,mission_summary
    payload=json.loads(row['payload']);questions=[];issues=[]
    payload.setdefault('bill_period',datetime.fromisoformat(row['created_at']).astimezone(b.TZ).strftime('%Y-%m'))
    if not payload['amount']:questions.append({'field':'amount','text':'请明确要预留多少人民币预算'})
    if payload['minimum_balance'] is None:questions.append({'field':'minimum_balance','text':'请明确房租或生活费需要保留的人民币金额'})
    if payload['cancel_subscription'] and not payload['subscription_id']:questions.append({'field':'subscription_name','text':'请写明需要取消的一个订阅商户，不能猜测或批量取消'})
    if payload['time_issue']:questions.append({'field':'not_before','text':'请明确最早执行的日期与时间；默认时间不会代替你的要求'})
    if payload['not_before']:
        when=datetime.fromisoformat(payload['not_before'])
        if when<=b._now() or when>b._now()+timedelta(days=7):issues.append('执行时间必须在未来七天内，请明确调整')
        if when>=datetime.fromisoformat(user['expires_at']):issues.append('执行时间超出当前沙箱会话有效期，请调整时间后重新核对')
    if row['expires_at']<=b._now().isoformat():issues.append('目标草案已过期，请重新整理需求')
    if payload['subscription_id'] and not db.execute("SELECT 1 FROM sandbox_subscriptions WHERE id=? AND session_id=? AND status='ACTIVE'",(payload['subscription_id'],user['id'])).fetchone():issues.append('指定订阅当前已不可取消，请重新核对需求')
    if payload['bill_source']=='imported' and not m.imported_snapshot(db,user['id'],payload['bill_period'])[1]:issues.append('没有已确认导入的目标月份人民币支出，请先导入并核对')
    if payload['amount'] and payload['minimum_balance'] is not None:
        floor=0 if payload['minimum_balance']=='0.00' else b._amount_fen(payload['minimum_balance'])
        enough=b._available(user)>=b._amount_fen(payload['amount'])+floor
        if not enough:issues.append('当前可用余额不足以同时满足预算和生活费底线；不会降低任一金额')
    state='COMPILED' if row['mission_id'] else 'BLOCKED' if payload['blockers'] or issues else 'NEEDS_INPUT' if questions else 'READY'
    if row['mission_id']:
        linked=m.owned(db,user['id'],row['mission_id'])
        monthly=mission_summary(db,linked,json.loads(linked['input_json']),json.loads(linked['nodes_json']))
    else:monthly=goal_summary(db,user,payload)
    return {'monthly':monthly,'id':row['id'],'revision':row['revision'],'digest':row['digest'],'expires_at':row['expires_at'],'status':state,'mission_id':row['mission_id'],
            'plan':payload,'questions':questions,'issues':issues,'checked_at':b._now().isoformat(),'available_balance':b._money(b._available(user)),
            'funds_authorized':False,'scope':'本月账单核查、一笔本地预算和一个指定订阅；异常处置以当前明确确认的范围为准。草案本身不授权执行，订阅取消始终等待复核。'}


@b.router.post('/goal-plans/prepare')
def prepare(req:GoalRequest,x_banking_session:str|None=Header(default=None)):
    with b._connect() as db:
        schema(db);db.execute('BEGIN IMMEDIATE');user=b._session(db,x_banking_session)
        if db.execute('SELECT COUNT(*) FROM banking_goal_proposals WHERE session_id=? AND mission_id IS NULL AND expires_at>?',(user['id'],b._now().isoformat())).fetchone()[0]>=20:raise HTTPException(429,'未完成目标草案过多，请先核对现有草案或等待过期')
        subscriptions=db.execute("SELECT id,merchant FROM sandbox_subscriptions WHERE session_id=? AND status='ACTIVE'",(user['id'],)).fetchall()
        payload=parse(req.text,subscriptions);identifier=secrets.token_hex(16);now=b._now()
        db.execute('INSERT INTO banking_goal_proposals VALUES(?,?,?,?,?,?,?,NULL)',(identifier,user['id'],json.dumps(payload,ensure_ascii=False),1,digest(identifier,user['id'],1,payload),now.isoformat(),(now+timedelta(minutes=30)).isoformat()))
        b._audit(db,user['id'],identifier,'GOAL_PROPOSAL_PREPARED',{'version':VERSION,'text_sha256':hashlib.sha256(req.text.encode()).hexdigest()})
        return view(db,user,owned(db,user['id'],identifier))


@b.router.get('/goal-plans/{identifier}')
def read(identifier:str,x_banking_session:str|None=Header(default=None)):
    with b._connect() as db:
        schema(db);user=b._session(db,x_banking_session)
        return view(db,user,owned(db,user['id'],identifier))


@b.router.patch('/goal-plans/{identifier}')
def edit(identifier:str,req:GoalEdit,x_banking_session:str|None=Header(default=None)):
    with b._connect() as db:
        schema(db);db.execute('BEGIN IMMEDIATE');user=b._session(db,x_banking_session);row=owned(db,user['id'],identifier)
        if row['mission_id'] or row['expires_at']<=b._now().isoformat():raise HTTPException(409,'草案已转为任务或过期，请在对应任务中核对')
        if row['revision']>=30:raise HTTPException(409,'本草案修改次数已达上限，请重新整理需求')
        if not secrets.compare_digest(req.digest,row['digest']):raise HTTPException(409,'草案已变化，请重新读取后修改')
        payload=json.loads(row['payload']);values=req.model_dump(exclude_unset=True);values.pop('digest')
        updates={}
        if req.risk_policy is not None and req.risk_policy!=payload.get('risk_policy','pause_all_on_suspicion'):
            if req.risk_policy=='reserve_before_review' and not req.acknowledge_policy_change:raise HTTPException(422,'改变异常处置范围前，必须明确确认仅允许本地预算先预留')
            updates.update(risk_policy=req.risk_policy,risk_policy_acknowledged=req.risk_policy=='reserve_before_review')
        for field in ('amount','minimum_balance'):
            if field in values:
                value=values[field]
                if value is None:raise HTTPException(422,'金额不能留空')
                updates[field]='0.00' if field=='minimum_balance' and value in {'0','0.00'} else b._money(b._amount_fen(value))
        if 'subscription_name' in values:
            if not payload['cancel_subscription']:raise HTTPException(422,'原需求不包含取消订阅，不能通过补充信息新增操作')
            matches=db.execute("SELECT id,merchant FROM sandbox_subscriptions WHERE session_id=? AND merchant=? AND status='ACTIVE'",(user['id'],req.subscription_name)).fetchall()
            if len(matches)!=1:raise HTTPException(422,'未能唯一对应当前有效的订阅商户')
            updates.update(subscription_id=matches[0]['id'],subscription_name=matches[0]['merchant'])
        if 'execution_mode' in values or 'not_before' in values:
            if req.execution_mode=='immediate':updates.update(not_before=None,time_issue=None)
            elif req.not_before and req.not_before.tzinfo and b._now()<req.not_before<=b._now()+timedelta(days=7):updates.update(not_before=req.not_before.astimezone(b.TZ).isoformat(),time_issue=None)
            else:raise HTTPException(422,'请提供未来七天内且带时区的明确执行时间')
        updates={field:value for field,value in updates.items() if payload.get(field)!=value or (field=='not_before' and payload.get('time_issue'))}
        if not updates:raise HTTPException(422,'没有可保存的修改')
        for field,value in updates.items():
            if field in {'time_issue','subscription_id','risk_policy_acknowledged'}:continue
            payload['changes'].append({'field':field,'before':payload.get(field),'after':value})
            payload['provenance'][field]={'source':'user_edit','quote':'由你在草案中明确修改'}
        payload.update(updates);revision=row['revision']+1
        db.execute('UPDATE banking_goal_proposals SET payload=?,revision=?,digest=? WHERE id=?',(json.dumps(payload,ensure_ascii=False),revision,digest(identifier,user['id'],revision,payload),identifier))
        b._audit(db,user['id'],identifier,'GOAL_PROPOSAL_REVISED',{'revision':revision,'fields':sorted(updates)})
        return view(db,user,owned(db,user['id'],identifier))


@b.router.post('/goal-plans/{identifier}/compile')
def compile_goal(identifier:str,req:Compile,x_banking_session:str|None=Header(default=None)):
    with b._connect() as db:
        schema(db);db.execute('BEGIN IMMEDIATE');user=b._session(db,x_banking_session);row=owned(db,user['id'],identifier)
        return compile_in_db(db,user,row,req.digest)


def compile_in_db(db,user,row,expected_digest):
    identifier=row['id']
    if not secrets.compare_digest(expected_digest,row['digest']):raise HTTPException(409,'草案摘要已变化，请核对最新条件')
    if row['mission_id']:return m.view(db,m.owned(db,user['id'],row['mission_id']))
    current=view(db,user,row)
    if current['status']!='READY':raise HTTPException(409,'目标仍有缺失、冲突或无法支持的条件，尚未创建任务')
    payload=current['plan']
    mission=m.prepare_in_db(db,user,m.MissionRequest(amount=payload['amount'],minimum_balance=payload['minimum_balance'],cancel_subscription=payload['cancel_subscription'],subscription_id=payload['subscription_id'],bill_source=payload['bill_source']),
        {'goal_proposal_id':identifier,'bill_period':payload['bill_period'],'not_before':payload['not_before'],'constraint_provenance':payload['provenance'],'risk_policy':payload['risk_policy'],'risk_policy_acknowledged':payload.get('risk_policy_acknowledged',False)})
    db.execute('UPDATE banking_goal_proposals SET mission_id=? WHERE id=?',(mission['id'],identifier))
    b._audit(db,user['id'],mission['id'],'GOAL_CONSTRAINTS_BOUND',{'goal_id':identifier,'goal_revision':row['revision'],'digest':mission['digest']})
    return mission


@b.router.post('/goal-plans/{identifier}/activate')
def activate(identifier:str,req:Activate,x_banking_session:str|None=Header(default=None)):
    if not req.confirmed:raise HTTPException(422,'请先核对并明确确认月度安排')
    with b._connect() as db:
        schema(db);db.execute('BEGIN IMMEDIATE');user=b._session(db,x_banking_session);row=owned(db,user['id'],identifier)
        mission=compile_in_db(db,user,row,req.digest)
        current=m.owned(db,user['id'],mission['id'])
        if current['status']!='DRAFT':return m.view(db,current)
        payload=view(db,user,row)['plan'];inputs=json.loads(current['input_json'])
        for field in ('amount','minimum_balance','subscription_id','cancel_subscription','bill_source','bill_period','not_before','risk_policy'):
            if inputs.get(field)!=payload.get(field):raise HTTPException(409,'执行计划已单独修改，请到计划详情核对最新内容后确认')
        m.validate_confirmation(db,user,current,current['digest'])
        db.execute("UPDATE banking_missions SET status='RUNNING',updated_at=? WHERE id=?",(b._now().isoformat(),current['id']))
        m.event(db,current,'MISSION_CONFIRM',{'from':'DRAFT','to':'RUNNING','confirmation_surface':'monthly_arrangement','goal_revision':row['revision'],'risk_policy':inputs['risk_policy']})
        return m.view(db,m.owned(db,user['id'],current['id']))
