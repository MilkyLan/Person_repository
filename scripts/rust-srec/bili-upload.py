#!/usr/bin/env python3
"""rust-srec Execute 处理器预设：程序 python3；参数 脚本路径、{session_id}、{streamer}、{inputs_json}。"""
import json
import shlex
import re
import time
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ===== 用户配置（无需 Shell / export） =====
CONFIG = {
    "DB": "/app/data/rust-srec.db",
    "BILIUP_BIN": "/app/config/biliup",
    "COOKIE": "/app/config/cookies-qn.json",
    "MEDIA_EXTS": ".flv",
    "STREAMER_GROUPS": {
        "不会捏蓝": ["不会捏蓝", "薄荷奶绿"],
        "克烈": ["克烈", "麻辣毛蛋"],
        "南一": ["南一", "周周大魔王"],
        "在下失陪":["在下失陪","开法拉利的辣妹"]
    },
    "TAG": "直播回放,直播录像,录播,{streamer},{streamer_tags},{streamer}直播回放,{streamer}直播录像",
    "DESC": "",
    "TID": "171",
    "COPYRIGHT": 1,
    "SOURCE": "",  # 转载来源；空字符串不传

    # ===== biliup 可选投稿字段：None / "" = 不传 =====
    "SUBMIT": None,          # app / web / b-cut-android；None 使用 biliup 默认值
    "PROXY": None,           # 代理 URL；全局选项
    "RUST_LOG": None,        # 例如 "debug"；全局选项
    "COVER": None,           # 封面 URL
    "DYNAMIC": None,         # 空间动态文案，支持 {streamer} 等模板变量
    "DTIME": None,           # Unix 秒时间戳，定时发布
    "TID_V2": None,          # 新分区 ID
    "INTERACTIVE": None,     # 0 或 1，0 也会明确传参
    "MISSION_ID": None,      # 投稿活动 ID
    "DOLBY": None,           # 0 或 1
    "HIRES": None,           # 0 或 1
    "NO_REPRINT": None,      # 0 允许转载 / 1 禁止转载
    "CHARGING_PAY": None,    # 0 或 1
    "IS_ONLY_SELF": None,    # 0 或 1
    "UP_SELECTION_REPLY": False, # True 才添加布尔开关（仅 submit=app）
    "UP_CLOSE_REPLY": False,
    "UP_CLOSE_DANMU": False,
    "EXTRA_FIELDS": None,    # dict 或 JSON 字符串；空 dict / 空字符串不传

    "LINE": "txa",
    "LIMIT": 10,
    "SPEED_LIMIT": 0,
    "TITLE_TEMPLATE": "【{streamer}】{date} 接录播 直播回放",
    "TITLE_SUFFIX": "",
    "FAIL_EXIT": 1,
    "MAX_ATTEMPTS": 2,  # 首次尝试 + 自动重试1次
    "RETRY_WAIT": 10,    # 秒
    "DATE_TZ": "Asia/Shanghai",
}

if len(sys.argv) != 4:
    print('用法: python3 bili-upload.py <session_id> <streamer> <inputs_json>', file=sys.stderr)
    sys.exit(2)

DB=CONFIG['DB']; SID=sys.argv[1]; NAME=sys.argv[2]
TZ=ZoneInfo(CONFIG['DATE_TZ'])
BV_RE=re.compile(r'\bBV[0-9A-Za-z]{10}\b')

def log(s): print('[bili-upload] '+str(s),flush=True)
def die(s):
    print('[bili-upload] ERROR: '+str(s),file=sys.stderr,flush=True)
    sys.exit(1 if str(CONFIG['FAIL_EXIT'])=='1' else 0)

def grouped():
    groups = CONFIG['STREAMER_GROUPS']
    if not isinstance(groups, dict):
        raise ValueError('STREAMER_GROUPS 必须是字典，格式：{"组名": ["主播1", "主播2"]}')
    membership = {}
    for group, names in groups.items():
        if not isinstance(group, str) or not group.strip():
            raise ValueError('主播组名不能为空')
        if not isinstance(names, (list, tuple)) or not names:
            raise ValueError(f'主播组 {group!r} 的成员必须是非空列表')
        for name in names:
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f'主播组 {group!r} 包含无效主播名称')
            if name in membership:
                raise ValueError(f'主播重复分组: {name}')
            membership[name] = group
    group = membership.get(NAME, NAME)
    return group, list(groups[group]) if group in groups else [NAME]

def session_date(value):
    if value is None: raise ValueError('会话 start_time 为空')
    v=str(value).strip()
    if re.fullmatch(r'\d+(?:\.\d+)?',v):
        n=float(v)
        if n>1e11: n/=1000
        dt=datetime.fromtimestamp(n,timezone.utc)
    else:
        dt=datetime.fromisoformat(v.replace('Z','+00:00'))
        if dt.tzinfo is None: dt=dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).date().isoformat()

def columns(con,table):
    return {r['name'] for r in con.execute('PRAGMA table_info('+table+')')}

def schema(con):
    cs=columns(con,'live_sessions')
    if not {'id','start_time'}<=cs: raise RuntimeError('live_sessions 不含 id/start_time，停止迁移')
    if 'bvid' not in cs:
        log('迁移：ALTER TABLE live_sessions ADD COLUMN bvid TEXT')
        con.execute('ALTER TABLE live_sessions ADD COLUMN bvid TEXT')
    con.execute('''CREATE TABLE IF NOT EXISTS bili_upload_segment_state (
       session_id TEXT NOT NULL, file_path TEXT NOT NULL,
       status TEXT NOT NULL CHECK(status IN ('inflight','done','unknown')),
       bvid TEXT, file_size INTEGER NOT NULL,
       updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
       PRIMARY KEY(session_id,file_path))''')
    con.commit()

def name_resolver(con):
    cs=columns(con,'live_sessions')
    for col in ('streamer_name','streamer'):
        if col in cs:
            return ('direct',col)
    if 'streamer_id' in cs and {'id','name'}<=columns(con,'streamers'):
        return ('join','streamer_id')
    raise RuntimeError('无法从数据库关联主播名：请提供 PRAGMA table_info(live_sessions) 和 streamers')

def lookup_sessions(con,member_names,resolver):
    # 只获取这一组的录制会话；日期全部按各自 start_time 转换，完全不使用分段日期
    placeholders=','.join('?' for _ in member_names)
    if resolver[0]=='direct':
        col=resolver[1]
        sql=f'SELECT id,start_time,bvid,{col} AS session_streamer FROM live_sessions WHERE {col} IN ({placeholders})'
    else:
        sql=f'''SELECT ls.id,ls.start_time,ls.bvid,s.name AS session_streamer
          FROM live_sessions ls JOIN streamers s ON s.id=ls.streamer_id
          WHERE s.name IN ({placeholders})'''
    return con.execute(sql,member_names).fetchall()

def render_template(template, variables):
    """格式化投稿文案；未定义的占位符会明确报错。"""
    return str(template).format_map(variables)

def normalize_tags(rendered):
    """对英文逗号分隔的投稿标签去空白并去重，保留原顺序。"""
    return ','.join(dict.fromkeys(
        part.strip() for part in rendered.split(',') if part.strip()
    ))

def enabled(value):
    """None 和空字符串视为未配置，0/False 是明确的值。"""
    return value is not None and not (isinstance(value, str) and not value.strip())

def add_option(parts, flag, value):
    if enabled(value):
        parts.extend([flag, str(value)])

def cmd(media, bvid, title, tag, desc, source, variables):
    # 执行命令的所有参数均为列表，不经 shell 解释。
    prefix = []
    if int(str(CONFIG['SPEED_LIMIT'])) > 0:
        prefix = ['trickle', '-u', str(CONFIG['SPEED_LIMIT'])]
    parts = [CONFIG['BILIUP_BIN'], '--user-cookie', CONFIG['COOKIE']]
    add_option(parts, '--proxy', CONFIG['PROXY'])
    add_option(parts, '--rust-log', CONFIG['RUST_LOG'])

    if bvid:
        # 追加只传目标 BVID 和传输配置，避免重设现有稿件的元数据。
        parts += ['append', '--vid', bvid]
    else:
        parts += ['upload', '--title', title, '--copyright', str(CONFIG['COPYRIGHT']),
                  '--tag', tag, '--tid', str(CONFIG['TID'])]
        add_option(parts, '--desc', desc)
        if str(CONFIG['COPYRIGHT']) == '2':
            add_option(parts, '--source', source)

        # 所有扩展稿件字段仅作用于新建投稿。
        textual = {'COVER': '--cover', 'DYNAMIC': '--dynamic'}
        for key, flag in textual.items():
            value = CONFIG[key]
            if enabled(value):
                add_option(parts, flag, render_template(value, variables))

        numeric = {
            'DTIME': '--dtime', 'TID_V2': '--tid-v2',
            'INTERACTIVE': '--interactive', 'MISSION_ID': '--mission-id',
            'DOLBY': '--dolby', 'HIRES': '--hires',
            'NO_REPRINT': '--no-reprint', 'CHARGING_PAY': '--charging-pay',
            'IS_ONLY_SELF': '--is-only-self',
        }
        for key, flag in numeric.items():
            value = CONFIG[key]
            if enabled(value):
                if isinstance(value, bool) or not str(value).strip().isdigit():
                    raise ValueError(f'{key} 必须为非负整数或 None')
                add_option(parts, flag, value)

        switches = {
            'UP_SELECTION_REPLY': '--up-selection-reply',
            'UP_CLOSE_REPLY': '--up-close-reply',
            'UP_CLOSE_DANMU': '--up-close-danmu',
        }
        if any(CONFIG[k] is True for k in switches):
            if CONFIG['SUBMIT'] not in (None, '', 'app'):
                raise ValueError('评论/弹幕控制开关仅支持 SUBMIT=app')
        for key, flag in switches.items():
            if CONFIG[key] is True:
                parts.append(flag)
            elif CONFIG[key] not in (None, False):
                raise ValueError(f'{key} 只允许 True、False 或 None')

        extras = CONFIG['EXTRA_FIELDS']
        if isinstance(extras, dict) and extras:
            add_option(parts, '--extra-fields', json.dumps(extras, ensure_ascii=False, separators=(',', ':')))
        elif isinstance(extras, str) and extras.strip():
            parsed = json.loads(extras)
            if not isinstance(parsed, dict):
                raise ValueError('EXTRA_FIELDS 必须为 JSON 对象')
            if parsed:
                add_option(parts, '--extra-fields', json.dumps(parsed, ensure_ascii=False, separators=(',', ':')))
        elif extras is not None and extras != {} and extras != '':
            raise ValueError('EXTRA_FIELDS 必须是字典、JSON 对象字符串或 None')

    # SUBMIT/LINE/LIMIT 是上传传输选项，upload 和 append 都可使用。
    add_option(parts, '--submit', CONFIG['SUBMIT'])
    add_option(parts, '--line', CONFIG['LINE'])
    if media.suffix.lower() == '.flv':
        add_option(parts, '--limit', CONFIG['LIMIT'])
    return prefix + parts + [str(media)]

def record_success(con, path, size, bvid):
    # 写入会话 BVID + 分段完成状态，两个操作使用同一事务。
    con.execute('BEGIN IMMEDIATE')
    con.execute('UPDATE live_sessions SET bvid=? WHERE id=? AND (bvid IS NULL OR bvid=? OR bvid=\'\')', (bvid, SID, bvid))
    row=con.execute('SELECT bvid FROM live_sessions WHERE id=?', (SID,)).fetchone()
    if not row or row['bvid'] != bvid:
        con.rollback()
        raise RuntimeError('当前会话 BVID 与投稿结果不一致，请核查')
    con.execute('''INSERT INTO bili_upload_segment_state
                   (session_id,file_path,status,bvid,file_size,updated_at)
                   VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(session_id,file_path) DO UPDATE SET
                   status='done',bvid=excluded.bvid,file_size=excluded.file_size,
                   updated_at=CURRENT_TIMESTAMP''', (SID,path,'done',bvid,size))
    con.commit()


def attempt(con,media,bvid,title,tag,desc,source,variables):
    path=str(media); size=media.stat().st_size
    if size<=0: raise RuntimeError('空视频: '+path)
    prior=con.execute('SELECT status,file_size,bvid FROM bili_upload_segment_state WHERE session_id=? AND file_path=?',(SID,path)).fetchone()
    if prior and prior['status']=='done':
        if prior['file_size'] != size: raise RuntimeError('已上传文件大小变化，请核查: '+path)
        if bvid and prior['bvid'] != bvid: raise RuntimeError('已上传分段 BVID 不一致: '+path)
        log('跳过已上传分段: '+media.name)
        return bvid or prior['bvid']

    # 旧版本留下的 unknown/inflight 状态不再阻止启动；重新调用即重新尝试。
    if prior:
        log('检测到上次未完成状态，允许重新尝试: '+media.name+' ('+prior['status']+')')
    con.execute('''INSERT INTO bili_upload_segment_state(session_id,file_path,status,bvid,file_size,updated_at)
                   VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(session_id,file_path) DO UPDATE SET
                   status='inflight',file_size=excluded.file_size,updated_at=CURRENT_TIMESTAMP''',
                (SID,path,'inflight',bvid,size))
    con.commit()
    command=cmd(media,bvid,title,tag,desc,source,variables)
    attempts=max(1,int(CONFIG['MAX_ATTEMPTS']))
    for index in range(1,attempts+1):
        log(f'{"追加" if bvid else "首投"} {index}/{attempts}: {media.name}')
        # 仅用于展示可复制的命令；实际执行仍然使用参数列表，不经过 Shell。
        def quote_media_arg(value):
            return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('`', '\\`') + '"'
        display_cmd = ' '.join(
            quote_media_arg(str(arg)) if pos == len(command) - 1 else shlex.quote(str(arg))
            for pos, arg in enumerate(command)
        )
        log('执行命令: ' + display_cmd)
        try:
            proc=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,errors='replace')
            output=proc.stdout or ''
            if output: print(output,end='' if output.endswith('\n') else '\n',flush=True)
            rc=proc.returncode
        except OSError as exc:
            rc=-1
            output=''
            log(f'启动 biliup 失败: {exc}')
        # 首投和追加均以 biliup 返回的 BVID 作为成功依据。
        # 追加时还必须与目标 BVID 一致；仅退出码为 0 不足以认定成功。
        returned_bvids = set(BV_RE.findall(output))
        if not bvid:
            if len(returned_bvids) == 1:
                new_bvid = next(iter(returned_bvids))
                record_success(con, path, size, new_bvid)
                log(f'首投成功：返回 BVID={new_bvid}（退出码={rc}）')
                return new_bvid
            if len(returned_bvids) > 1:
                log(f'首投返回多个不同 BVID，无法确定稿件（退出码={rc}）')
            else:
                log(f'首投未返回 BVID（退出码={rc}）')
        else:
            if returned_bvids == {bvid}:
                record_success(con, path, size, bvid)
                log(f'追加成功：返回 BVID={bvid}（退出码={rc}）')
                return bvid
            if returned_bvids:
                log(f'追加 BVID 不匹配：预期={bvid}，返回={sorted(returned_bvids)}（退出码={rc}）')
            else:
                log(f'追加未返回 BVID（退出码={rc}）')
        if index<attempts:
            wait=max(0,float(CONFIG['RETRY_WAIT']))
            log(f'{wait:g} 秒后自动重试一次')
            time.sleep(wait)
    # 保留失败状态用于观察，但下次 Execute 调用不阻止手动重试。
    con.execute('''UPDATE bili_upload_segment_state SET status='unknown',updated_at=CURRENT_TIMESTAMP
                   WHERE session_id=? AND file_path=?''',(SID,path))
    con.commit()
    raise RuntimeError(f'{media.name} 投稿尝试 {attempts} 次仍未成功；可手动重新执行工作流')

try:
    group,members=grouped()
    inputs=json.loads(sys.argv[3])
    if not isinstance(inputs,list): raise ValueError('inputs_json 必须为路径数组')
    allowed={x.strip().lower() for x in CONFIG['MEDIA_EXTS'].split(',') if x.strip()}
    medias=sorted({Path(x).resolve() for x in inputs if isinstance(x,str) and not x.endswith('_inputs.json') and Path(x).is_file() and Path(x).suffix.lower() in allowed},key=lambda p:p.name)
    if not medias: raise RuntimeError('没有可上传的媒体文件')
    with sqlite3.connect(DB,timeout=30) as con:
        con.row_factory=sqlite3.Row
        con.execute('PRAGMA busy_timeout=30000')
        schema(con)
        resolver=name_resolver(con)
        current=con.execute('SELECT id,start_time,bvid FROM live_sessions WHERE id=?',(SID,)).fetchone()
        if not current: raise RuntimeError('找不到会话: '+SID)
        day=session_date(current['start_time'])
        candidates=lookup_sessions(con,members,resolver)
        matches=[r for r in candidates if session_date(r['start_time'])==day]
        if SID not in {r['id'] for r in matches}:
            raise RuntimeError('当前会话主播与工作流传入的 streamer 或主播分组不匹配')
        # 按会话开始时间排序，同一天全组必须共用同一个BVID
        matches.sort(key=lambda r:(int(r['start_time']),str(r['id'])))
        existing={r['bvid'].strip() for r in matches if isinstance(r['bvid'],str) and r['bvid'].strip()}
        if len(existing)>1: raise RuntimeError('同组同日会话存在不同BVID：'+', '.join(sorted(existing)))
        own=(current['bvid'] or '').strip()
        shared=next(iter(existing),None)
        if own and shared and own!=shared: raise RuntimeError('当前会话 BVID 与同组同日 BVID 不一致')
        bvid=own or shared
        log(f'会话={SID} 主播={NAME} 组={group} 日期={day} 同组同日会话={len(matches)} BVID={bvid or "未创建"}')
        # 无 BVID 时允许重新首投：不再因同组旧 unknown/inflight 状态阻止手动重试。
        # 注意远端成功而无 BVID 返回的极端情况可能造成重复投稿。
        if bvid and not own:
            con.execute('UPDATE live_sessions SET bvid=? WHERE id=? AND (bvid IS NULL OR bvid=\'\')',(bvid,SID))
            con.commit()
            log('当前会话继承同组同日 BVID: '+bvid)
        # 模板使用统一变量：分组主播取组名，未分组主播取原始主播名。
        variables = {
            'streamer': group,
            'streamer_tags': ','.join(members),
            'date': day,
        }
        title = render_template(CONFIG['TITLE_TEMPLATE'], variables)
        if CONFIG['TITLE_SUFFIX']:
            title += ' ' + render_template(CONFIG['TITLE_SUFFIX'], variables)
        tag = normalize_tags(render_template(CONFIG['TAG'], variables))
        desc = render_template(CONFIG['DESC'], variables)
        source = render_template(CONFIG['SOURCE'], variables)
        for media in medias:
            bvid=attempt(con,media,bvid,title,tag,desc,source,variables)
        log(f'本次工作流结束: {SID} -> {bvid}')
except Exception as exc:
    die(exc)
