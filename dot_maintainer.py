# -*- coding: utf-8 -*-
from __future__ import annotations
import json
import os
import re
import queue
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from tool_paths import TOOL_DIR, catalog_path, find_project_root, raw_bundles_dir, runtime_dir
from parallel_utils import IO_WORKERS, CPU_WORKERS
from catalog_asset_tool import DEFAULT_CATALOG_URL, parse_catalog_url, build_catalog_records, character_bundle_names
from resource_table_store import save_resource_tables

APP_TITLE = 'Dot 离线资源维护器 1.3'
CONFIG_PATH = TOOL_DIR / 'settings.json'
REPORT_DIR = TOOL_DIR / 'reports'
CDN_GUIDE = (
    '粘贴完整 catalog_1.bin 下载地址后会自动识别版本和 Bundle 基础地址。\n'
    '资源表下载使用该地址对应的 /aa/ 目录，不要求手动拆分版本号。'
)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        # Adaptive window: use most of the screen without exceeding a comfortable desktop size.
        # Functional tabs are scrollable, so smaller/laptop displays still expose every action.
        sw=max(960,self.winfo_screenwidth()); sh=max(640,self.winfo_screenheight())
        ww=min(1360,max(980,int(sw*0.92))); wh=min(960,max(680,int(sh*0.90)))
        x=max(0,(sw-ww)//2); y=max(0,(sh-wh)//2)
        self.geometry(f'{ww}x{wh}+{x}+{y}')
        self.minsize(860, 620)
        self.proc=None
        self.log_queue=queue.Queue()
        self.running=False
        self.config_data=self._load_config()
        detected=find_project_root(TOOL_DIR)
        configured_project=str(self.config_data.get('project_root') or '').strip()
        if configured_project and Path(configured_project).expanduser().exists():
            initial_project=configured_project
        elif (detected/'data_r18_all').is_dir():
            initial_project=str(detected)
        else:
            initial_project=''
        self.project_var=tk.StringVar(value=initial_project)
        cat_cfg=self.config_data.get('catalog') or str(catalog_path())
        cat_candidate=Path(cat_cfg).expanduser()
        if not cat_candidate.is_absolute(): cat_candidate=TOOL_DIR/cat_candidate
        if not cat_candidate.is_file(): cat_candidate=catalog_path()
        self.catalog_var=tk.StringVar(value=str(cat_candidate.resolve()))
        self.catalog_url_var=tk.StringVar(value=self.config_data.get('catalog_url') or DEFAULT_CATALOG_URL)
        parsed_initial={}
        try: parsed_initial=parse_catalog_url(self.catalog_url_var.get())
        except Exception: parsed_initial={}
        version_initial=(parsed_initial.get('version') if parsed_initial.get('version','').isdigit() else '') or self.config_data.get('cdn_version','')
        self.version_var=tk.StringVar(value=version_initial)
        self.catalog_url_status_var=tk.StringVar(value=(f"已识别版本 {version_initial}，Bundle 地址：{parsed_initial.get('base_url','')}" if parsed_initial else '请输入完整的 catalog_1.bin 下载地址。'))
        self.catalog_base_url=parsed_initial.get('base_url','')
        self.asset_library_var=tk.StringVar(value=self.config_data.get('asset_library_root') or str(TOOL_DIR/'catalog_asset_library'))
        self.raw_bundle_root_var=tk.StringVar(value=self.config_data.get('raw_bundle_root') or str(TOOL_DIR/'raw_bundles'))
        self.resource_table_root_var=tk.StringVar(value=self.config_data.get('resource_table_root') or str(TOOL_DIR/'resource_tables'))
        self.extract_original_var=tk.BooleanVar(value=bool(self.config_data.get('extract_after_download', True)))
        self.include_common_var=tk.BooleanVar(value=bool(self.config_data.get('include_common_resources', True)))
        try: configured_workers=int(self.config_data.get('download_workers', IO_WORKERS) or IO_WORKERS)
        except Exception: configured_workers=IO_WORKERS
        self.download_workers_var=tk.IntVar(value=max(1,min(64,configured_workers)))
        self.resource_selected_names=set()
        self._rendering_resource_tree=False
        self.job_workspace='player'
        self.resource_table_summary_var=tk.StringVar(value='资源表尚未保存。')
        self.resource_query_var=tk.StringVar(value='')
        self.resource_kind_var=tk.StringVar(value='全部类型')
        self.resource_scope_var=tk.StringVar(value='全部范围')
        self.resource_character_var=tk.StringVar(value='')
        self.resource_status_var=tk.StringVar(value='资源表尚未载入。')
        self.resource_records=[]
        self.resource_filtered_records=[]
        self.resource_iid_to_record={}
        self.resource_render_index=0
        self.resource_render_generation=0
        self.resource_page_index=0
        self.resource_page_size=500
        self.resource_page_records=[]
        self.resource_page_var=tk.StringVar(value='第 1 / 1 页')
        self.resource_filter_job=None
        self.resource_render_after=None
        self.auto_index_var=tk.BooleanVar(value=bool(self.config_data.get('auto_update_index', True)))
        self.audit_summary_var=tk.StringVar(value='尚未执行资源检查。')
        self.latest_audit_path=REPORT_DIR/'resource_audit_latest.json'
        self.full_story_summary_var=tk.StringVar(value='尚未扫描 Catalog 全量剧情映射。')
        self.status_var=tk.StringVar(value='就绪')
        self.progress_stage_var=tk.StringVar(value='等待任务')
        self.progress_detail_var=tk.StringVar(value='尚未开始')
        self.progress_percent_var=tk.StringVar(value='0%')
        self.progress_value=tk.DoubleVar(value=0.0)
        self._style(); self._ui(); self.catalog_url_var.trace_add('write', self._catalog_url_changed); self._drain_after_id=self.after(80,self._drain); self._audit_after_id=self.after(140,self._load_cached_audit_summary)
        self.protocol('WM_DELETE_WINDOW',self._close)
        self._startup_resource_load_id=self.after(450, self._load_resource_table_on_startup)

    def _load_config(self):
        try: return json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
        except Exception: return {}

    def _save_config(self):
        data={
            'project_root':self.project_var.get().strip(),
            'catalog':self.catalog_var.get().strip(),
            'cdn_version':self.version_var.get().strip(),
            'catalog_url':self.catalog_url_var.get().strip(),
            'catalog_base_url':self.catalog_base_url,
            'asset_library_root':self.asset_library_var.get().strip(),
            'raw_bundle_root':self.raw_bundle_root_var.get().strip(),
            'resource_table_root':self.resource_table_root_var.get().strip(),
            'extract_after_download':bool(self.extract_original_var.get()),
            'include_common_resources':bool(self.include_common_var.get()),
            'download_workers':self._io_workers(),
            'auto_update_index':bool(self.auto_index_var.get()),
        }
        try: CONFIG_PATH.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        except Exception: pass

    def _style(self):
        self.configure(bg='#0d1424')
        st=ttk.Style(self)
        try: st.theme_use('clam')
        except Exception: pass
        st.configure('Root.TFrame',background='#0d1424')
        st.configure('Card.TFrame',background='#151f33')
        st.configure('TLabel',background='#0d1424',foreground='#e6edf7',font=('Microsoft YaHei UI',10))
        st.configure('Card.TLabel',background='#151f33',foreground='#e6edf7',font=('Microsoft YaHei UI',10))
        st.configure('Muted.TLabel',background='#0d1424',foreground='#9aa9bf',font=('Microsoft YaHei UI',9))
        st.configure('CardMuted.TLabel',background='#151f33',foreground='#a8b4c7',font=('Microsoft YaHei UI',9))
        st.configure('Title.TLabel',background='#0d1424',foreground='#fff',font=('Microsoft YaHei UI',20,'bold'))
        st.configure('Section.TLabel',background='#151f33',foreground='#fff',font=('Microsoft YaHei UI',11,'bold'))
        st.configure('TButton',font=('Microsoft YaHei UI',10),padding=(11,7))
        st.configure('Accent.TButton',font=('Microsoft YaHei UI',10,'bold'),padding=(13,8))
        st.configure('Danger.TButton',font=('Microsoft YaHei UI',10,'bold'),padding=(13,8))
        st.configure('TEntry',fieldbackground='#0a1120',foreground='#f8fafc',insertcolor='#fff',padding=6)
        st.configure('TNotebook',background='#0d1424',borderwidth=0)
        st.configure('TNotebook.Tab',font=('Microsoft YaHei UI',10,'bold'),padding=(16,8))
        st.configure('TLabelframe',background='#151f33',foreground='#fff')
        st.configure('TLabelframe.Label',background='#151f33',foreground='#fff',font=('Microsoft YaHei UI',10,'bold'))

    def _ui(self):
        outer=ttk.Frame(self,style='Root.TFrame',padding=16); outer.pack(fill='both',expand=True)
        head=ttk.Frame(outer,style='Root.TFrame'); head.pack(fill='x')
        ttk.Label(head,text=APP_TITLE,style='Title.TLabel').pack(side='left')
        ttk.Button(head,text='设置',command=self.open_settings).pack(side='right',padx=(8,0))
        ttk.Button(head,text='打开提取目录',command=self.open_asset_library).pack(side='right',padx=(8,0))
        ttk.Button(head,text='打开资源表目录',command=self.open_resource_tables).pack(side='right',padx=(8,0))
        self.busy=ttk.Progressbar(head,mode='indeterminate',length=150); self.busy.pack(side='right',padx=(10,0))
        ttk.Label(head,textvariable=self.status_var,style='Muted.TLabel').pack(side='right')
        ttk.Label(outer,text='播放器维护与独立人物资源提取分区运行；Catalog、路径、下载并发和提取策略统一在“设置”中调整。',style='Muted.TLabel').pack(anchor='w',pady=(3,8))

        progress=ttk.Frame(outer,style='Card.TFrame',padding=(12,9)); progress.pack(fill='x',pady=(0,10))
        prow=ttk.Frame(progress,style='Card.TFrame'); prow.pack(fill='x')
        ttk.Label(prow,text='当前任务进度',style='Section.TLabel').pack(side='left')
        ttk.Label(prow,textvariable=self.progress_stage_var,style='Card.TLabel').pack(side='left',padx=(12,0))
        ttk.Label(prow,textvariable=self.progress_percent_var,style='Card.TLabel').pack(side='right')
        self.task_progress=ttk.Progressbar(progress,mode='determinate',maximum=100,variable=self.progress_value)
        self.task_progress.pack(fill='x',pady=(7,4))
        ttk.Label(progress,textvariable=self.progress_detail_var,style='CardMuted.TLabel',justify='left',wraplength=1050).pack(anchor='w')

        self.workspace_tabs=ttk.Notebook(outer); self.workspace_tabs.pack(fill='both',expand=True)
        self.player_workspace=ttk.Frame(self.workspace_tabs,style='Root.TFrame')
        self.extract_workspace=ttk.Frame(self.workspace_tabs,style='Root.TFrame')
        self.workspace_tabs.add(self.player_workspace,text='播放器维护')
        self.workspace_tabs.add(self.extract_workspace,text='独立人物资源提取')

        # Workspace 1: retain all legacy player-specific workflows together.
        player_wrap=ttk.Frame(self.player_workspace,style='Root.TFrame',padding=(6,8,6,6)); player_wrap.pack(fill='both',expand=True)
        self.player_tabs=ttk.Notebook(player_wrap); self.player_tabs.pack(fill='both',expand=True)
        self.download_page=ttk.Frame(self.player_tabs,style='Root.TFrame')
        self.audit_page=ttk.Frame(self.player_tabs,style='Root.TFrame')
        self.maint_page=ttk.Frame(self.player_tabs,style='Root.TFrame')
        self.log_tab=ttk.Frame(self.player_tabs,style='Root.TFrame',padding=10)
        self.player_tabs.add(self.download_page,text='下载与处理')
        self.player_tabs.add(self.audit_page,text='资源检查 / 自动修复')
        self.player_tabs.add(self.maint_page,text='索引 / 公共资源整理')
        self.player_tabs.add(self.log_tab,text='运行日志')
        self.download_tab=self._scrollable_page(self.download_page)
        self.audit_tab=self._scrollable_page(self.audit_page)
        self.maint_tab=self._scrollable_page(self.maint_page)
        self._download_ui(); self._audit_ui(); self._maint_ui(); self._log_ui(self.log_tab, asset=False)

        # Workspace 2: resource catalog and extraction jobs are isolated from player maintenance.
        asset_wrap=ttk.Frame(self.extract_workspace,style='Root.TFrame',padding=(6,8,6,6)); asset_wrap.pack(fill='both',expand=True)
        self.asset_tabs=ttk.Notebook(asset_wrap); self.asset_tabs.pack(fill='both',expand=True)
        self.resource_page=ttk.Frame(self.asset_tabs,style='Root.TFrame')
        self.asset_log_tab=ttk.Frame(self.asset_tabs,style='Root.TFrame',padding=10)
        self.asset_tabs.add(self.resource_page,text='资源表与指定下载')
        self.asset_tabs.add(self.asset_log_tab,text='提取任务日志')
        self._resource_browser_ui(self.resource_page)
        self._log_ui(self.asset_log_tab, asset=True)

    def _scrollable_page(self,parent):
        """Create a vertically scrollable notebook page for small/high-DPI displays."""
        holder=ttk.Frame(parent,style='Root.TFrame')
        holder.pack(fill='both',expand=True)
        canvas=tk.Canvas(holder,bg='#0d1424',highlightthickness=0,borderwidth=0)
        scroll=ttk.Scrollbar(holder,orient='vertical',command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y')
        canvas.pack(side='left',fill='both',expand=True)
        inner=ttk.Frame(canvas,style='Root.TFrame',padding=(10,10,10,18))
        window=canvas.create_window((0,0),window=inner,anchor='nw')

        def sync_region(_event=None):
            canvas.configure(scrollregion=canvas.bbox('all'))
        def sync_width(event):
            canvas.itemconfigure(window,width=max(1,event.width))
        def wheel(event):
            delta=getattr(event,'delta',0)
            if delta:
                canvas.yview_scroll(-1 if delta>0 else 1,'units')
            elif getattr(event,'num',None)==4:
                canvas.yview_scroll(-1,'units')
            elif getattr(event,'num',None)==5:
                canvas.yview_scroll(1,'units')
            return 'break'
        def bind_wheel(_event=None):
            canvas.bind_all('<MouseWheel>',wheel)
            canvas.bind_all('<Button-4>',wheel)
            canvas.bind_all('<Button-5>',wheel)
        def unbind_wheel(_event=None):
            canvas.unbind_all('<MouseWheel>')
            canvas.unbind_all('<Button-4>')
            canvas.unbind_all('<Button-5>')

        inner.bind('<Configure>',sync_region)
        canvas.bind('<Configure>',sync_width)
        canvas.bind('<Enter>',bind_wheel)
        canvas.bind('<Leave>',unbind_wheel)
        inner.bind('<Enter>',bind_wheel)
        inner.bind('<Leave>',unbind_wheel)
        return inner

    def _card(self,parent,title,desc=None):
        f=ttk.Frame(parent,style='Card.TFrame',padding=14); f.pack(fill='x',pady=(0,10))
        ttk.Label(f,text=title,style='Section.TLabel').pack(anchor='w')
        if desc: ttk.Label(f,text=desc,style='CardMuted.TLabel',justify='left',wraplength=780).pack(anchor='w',pady=(3,10))
        return f

    def _download_ui(self):
        card=self._card(self.download_tab,'按 ID 下载 / 一键下载并处理','保留原下载器功能。所有原始 Bundle 统一缓存在维护器目录 raw_bundles 中，不再按任务分文件夹；同名 Bundle 已存在时直接复用。下载并处理只处理本次清单，不扫描历史缓存。支持角色 ID、11 位剧情 ID、BGM/SE/BG 等具体资源 ID。')
        self.ids_text=tk.Text(card,height=5,bg='#0a1120',fg='#e6edf7',insertbackground='#fff',relief='flat',font=('Consolas',10))
        self.ids_text.pack(fill='x')
        opts=ttk.Frame(card,style='Card.TFrame'); opts.pack(fill='x',pady=(10,0))
        ttk.Checkbutton(opts,text='下载并处理后自动更新索引',variable=self.auto_index_var).pack(side='left')
        row=ttk.Frame(card,style='Card.TFrame'); row.pack(fill='x',pady=(10,0))
        ttk.Button(row,text='仅下载',command=lambda:self.download_ids(False)).pack(side='left')
        ttk.Button(row,text='下载并处理',style='Accent.TButton',command=lambda:self.download_ids(True)).pack(side='left',padx=8)
        ttk.Button(row,text='查看 Catalog 匹配（不下载）',command=self.list_matches).pack(side='left')
        ttk.Button(row,text='打开 Bundle 缓存',command=lambda:self.open_path(self._resolve_configured_path(self.raw_bundle_root_var.get(), TOOL_DIR/'raw_bundles'))).pack(side='left',padx=8)

        local=self._card(self.download_tab,'处理本地 Bundle','选择的 .bundle 会复制到维护器自己的统一 raw_bundles 缓存，并且只处理本次所选文件；不会扫描或转换缓存中的历史 Bundle。')
        ttk.Button(local,text='选择并处理本地 Bundle',style='Accent.TButton',command=self.process_local).pack(anchor='w')

    def _audit_ui(self):
        card=self._card(self.audit_tab,'全剧情资源体检（30 线程）','读取 data_r18_all/stories 下所有 story.json，按剧情实际引用检查公共背景、BGM、SE、Voice/BGV、剧本文本以及 Live2D moc/model3/texture/motion。只检查不会修改文件。')
        ttk.Label(card,textvariable=self.audit_summary_var,style='Card.TLabel').pack(anchor='w',pady=(0,10))
        row=ttk.Frame(card,style='Card.TFrame'); row.pack(fill='x')
        ttk.Button(row,text='检查全部剧情',style='Accent.TButton',command=self.audit_only).pack(side='left')
        ttk.Button(row,text='检查并自动补全缺失资源',command=self.audit_and_repair).pack(side='left',padx=8)
        ttk.Button(row,text='打开检查报告目录',command=lambda:self.open_path(REPORT_DIR)).pack(side='left')
        helpc=self._card(self.audit_tab,'自动补全策略','如果刚执行过“检查全部剧情”，随后点击“检查并自动补全”会直接复用上次报告，不再重新扫描全部 story.json；只定向复核报告中有问题的剧情。\n公共 BG/BGM/SE：先检查现有多路径/公共池，确实缺失才精确下载。剧情私有 Voice/BGV、Live2D/剧本文本：只选择与缺失类型对应的 Bundle，不再把整套剧情资源全部重新下载/转换。\n所有 Bundle 共用维护器目录 raw_bundles 扁平缓存，修复后只复检受影响剧情并合并回完整报告。')

        full=self._card(
            self.audit_tab,
            'Catalog 全量补充剧情包',
            '先从 Catalog 的人物立绘（charastand）确认 9 位角色 ID，再检索该角色对应的 11 位剧情 ID，建立“角色 → 剧情”映射并与本地 story.json 对比。\n'
            '“扫描映射”只生成缺口报告；“全量补充”会以人物立绘确认的 9 位角色 ID 为起点，对应检索 11 位剧情 ID，只下载缺失剧情的剧本、Voice/BGV、Live2D、缩略图，并补齐缺失人物立绘；随后调用现有流水线提取 story 实际引用的 BG/BGM/SE 并更新索引。已有完整剧情不会重复下载。下载使用最多 30 线程，顶部会实时显示当前阶段、数量和百分比。'
        )
        ttk.Label(full,textvariable=self.full_story_summary_var,style='Card.TLabel').pack(anchor='w',pady=(0,10))
        frow=ttk.Frame(full,style='Card.TFrame'); frow.pack(fill='x')
        ttk.Button(frow,text='扫描 Catalog 剧情映射',command=self.full_story_scan).pack(side='left')
        ttk.Button(frow,text='全量补充缺失剧情',style='Accent.TButton',command=self.full_story_repair).pack(side='left',padx=8)
        ttk.Button(frow,text='打开报告目录',command=lambda:self.open_path(REPORT_DIR)).pack(side='left')

    def _maint_ui(self):
        card=self._card(self.maint_tab,'索引更新','根据每个 story.json 实际引用重建 BGM/SE 公共索引、剧情索引和缩略图索引，同时修复缺失 cue.paths。')
        ttk.Button(card,text='更新 / 修复全部索引',style='Accent.TButton',command=lambda:self.maintenance('index')).pack(anchor='w')

        org=self._card(self.maint_tab,'公共资源整理','公共池位置：data/audio/bgm/decoded 与 data/audio/se/decoded/misc。任何删除操作只处理 SHA-256 完全相同的副本。')
        grid=ttk.Frame(org,style='Card.TFrame'); grid.pack(fill='x')
        actions=[
            ('只扫描重复资源','report','只统计，不改文件。建议首次先运行。'),
            ('建立公共池','pool','复制到公共池，保留旧副本，兼容性最高。'),
            ('硬链接去重（推荐）','link','旧路径继续存在，但相同内容只占一份磁盘空间。'),
            ('仅保留公共池','pool_only','迁入公共池后删除完全相同旧副本、清空无用空目录，并强制同步索引。'),
        ]
        for i,(label,key,desc) in enumerate(actions):
            box=ttk.Frame(grid,style='Card.TFrame',padding=(0,7)); box.grid(row=i,column=0,sticky='ew')
            ttk.Button(box,text=label,command=lambda k=key:self.maintenance(k)).pack(side='left')
            ttk.Label(box,text=desc,style='CardMuted.TLabel',wraplength=720).pack(side='left',padx=(12,0))
        grid.columnconfigure(0,weight=1)

    def _log_ui(self, parent, asset=False):
        top=ttk.Frame(parent,style='Root.TFrame'); top.pack(fill='x')
        if asset:
            ttk.Button(top,text='打开提取目录',command=self.open_asset_library).pack(side='left',padx=8)
            target='asset_log'
        else:
            ttk.Button(top,text='打开项目目录',command=lambda:self.open_path(Path(self.project_var.get()))).pack(side='left',padx=8)
            target='log'
        ttk.Button(top,text='清空日志',command=lambda t=target:self._clear_log(t)).pack(side='left')
        ttk.Button(top,text='停止当前任务',command=self.stop_task).pack(side='right')
        widget=tk.Text(parent,bg='#08101d',fg='#dce8f8',insertbackground='#fff',relief='flat',font=('Consolas',9),wrap='word')
        widget.pack(fill='both',expand=True,pady=(8,0))
        widget.tag_configure('error',foreground='#ff9b9b'); widget.tag_configure('ok',foreground='#8fe3a2'); widget.tag_configure('info',foreground='#8fc8ff')
        setattr(self, target, widget)

    def _catalog_url_changed(self, *_args):
        # Auto-recognize a full URL while the user edits/pastes it; incomplete
        # intermediate text is harmless and will not trigger a network request.
        try:
            parsed=parse_catalog_url(self.catalog_url_var.get())
            self.catalog_base_url=parsed['base_url']
            version=parsed.get('version','')
            if version.isdigit(): self.version_var.set(version)
            self.catalog_url_status_var.set(f"已识别 | 版本：{version or '未识别'} | Bundle 基础地址：{parsed['base_url']}")
            self._save_config()
        except Exception:
            self.catalog_base_url=''
            if hasattr(self,'catalog_url_status_var'):
                self.catalog_url_status_var.set('地址尚未完整识别；请检查是否以 catalog_1.bin 结尾。')

    def _parse_catalog_url(self, show_error=False):
        try:
            parsed=parse_catalog_url(self.catalog_url_var.get())
            self.catalog_base_url=parsed['base_url']
            version=parsed.get('version','')
            if version.isdigit(): self.version_var.set(version)
            self.catalog_url_status_var.set(f"已识别 | 版本：{version or '未识别'} | Bundle 基础地址：{parsed['base_url']}")
            self._save_config()
            return parsed
        except Exception as exc:
            self.catalog_base_url=''
            self.catalog_url_status_var.set(f'地址无效：{exc}')
            if show_error: messagebox.showwarning('Catalog 地址无效',str(exc))
            return None

    def download_catalog_from_url(self):
        parsed=self._parse_catalog_url(show_error=True)
        if not parsed: return
        raw_destination=self.catalog_var.get().strip()
        destination=Path(raw_destination).expanduser() if raw_destination else TOOL_DIR/'catalog_1.bin'
        if not destination.is_absolute(): destination=TOOL_DIR/destination
        cmd=self._py('catalog_asset_tool.py','--fetch-catalog','--url',self.catalog_url_var.get().strip(),'--destination',destination)
        self.run_commands([(cmd,{0})],'下载并验证 Catalog',callback=self._after_catalog_download)

    def _after_catalog_download(self):
        self.catalog_url_status_var.set(f'Catalog 已下载并通过 Bundle 名称校验：{self.catalog_var.get()}')
        self.load_resource_table()

    def _resource_browser_ui(self,parent):
        outer=ttk.Frame(parent,style='Root.TFrame',padding=10); outer.pack(fill='both',expand=True)
        head=ttk.Frame(outer,style='Root.TFrame'); head.pack(fill='x')
        ttk.Label(head,text='完整 Catalog 资源表',style='Title.TLabel').pack(side='left')
        ttk.Label(head,text='按 Bundle 原始键检索；支持多选下载、人物关联资源集与按 Unity 内部路径提取。',style='Muted.TLabel').pack(side='left',padx=(14,0))

        filters=ttk.Frame(outer,style='Card.TFrame',padding=10); filters.pack(fill='x',pady=(10,8))
        ttk.Label(filters,text='关键字 / 人物ID / 剧情ID / Bundle 名称',style='Card.TLabel').grid(row=0,column=0,sticky='w')
        q=ttk.Entry(filters,textvariable=self.resource_query_var,width=38); q.grid(row=1,column=0,sticky='ew',pady=(3,0))
        ttk.Label(filters,text='资源类别',style='Card.TLabel').grid(row=0,column=1,sticky='w',padx=(10,0))
        self.resource_kind_combo=ttk.Combobox(filters,textvariable=self.resource_kind_var,state='readonly',width=20,values=['全部类型']); self.resource_kind_combo.grid(row=1,column=1,padx=(10,0),pady=(3,0))
        ttk.Label(filters,text='资源范围',style='Card.TLabel').grid(row=0,column=2,sticky='w',padx=(10,0))
        self.resource_scope_combo=ttk.Combobox(filters,textvariable=self.resource_scope_var,state='readonly',width=13,values=['全部范围','公共','R18','未判定']); self.resource_scope_combo.grid(row=1,column=2,padx=(10,0),pady=(3,0))
        ttk.Button(filters,text='筛选',command=self._apply_resource_filter).grid(row=1,column=3,padx=(10,0),pady=(3,0))
        ttk.Button(filters,text='重置',command=self._reset_resource_filter).grid(row=1,column=4,padx=(6,0),pady=(3,0))
        self.resource_load_button=ttk.Button(filters,text='重新载入本地 Catalog',command=lambda:self.load_resource_table(save_delta=False)); self.resource_load_button.grid(row=1,column=5,padx=(6,0),pady=(3,0))
        filters.columnconfigure(0,weight=1)
        pagebar=ttk.Frame(outer,style='Root.TFrame'); pagebar.pack(fill='x',pady=(0,5))
        ttk.Label(pagebar,textvariable=self.resource_status_var,style='Muted.TLabel').pack(side='left',fill='x',expand=True)
        ttk.Button(pagebar,text='上一页',command=lambda:self._change_resource_page(-1)).pack(side='left',padx=(6,0))
        ttk.Label(pagebar,textvariable=self.resource_page_var,style='Muted.TLabel').pack(side='left',padx=8)
        ttk.Button(pagebar,text='下一页',command=lambda:self._change_resource_page(1)).pack(side='left')

        table_frame=ttk.Frame(outer,style='Root.TFrame'); table_frame.pack(fill='both',expand=True)
        columns=('name','kind','character_ids','story_ids','downloaded')
        self.resource_tree=ttk.Treeview(table_frame,columns=columns,show='headings',selectmode='extended',height=19)
        headings={'name':'资源名称 / Bundle（完整名称可在下方详情查看）','kind':'资源类型','character_ids':'人物ID','story_ids':'关联剧情ID','downloaded':'已下载'}
        widths={'name':720,'kind':190,'character_ids':115,'story_ids':135,'downloaded':78}
        for col in columns:
            self.resource_tree.heading(col,text=headings[col])
            self.resource_tree.column(col,width=widths[col],minwidth=75,stretch=(col=='name'))
        vs=ttk.Scrollbar(table_frame,orient='vertical',command=self.resource_tree.yview)
        hs=ttk.Scrollbar(table_frame,orient='horizontal',command=self.resource_tree.xview)
        self.resource_tree.configure(yscrollcommand=vs.set,xscrollcommand=hs.set)
        self.resource_tree.grid(row=0,column=0,sticky='nsew'); vs.grid(row=0,column=1,sticky='ns'); hs.grid(row=1,column=0,sticky='ew')
        table_frame.rowconfigure(0,weight=1); table_frame.columnconfigure(0,weight=1)
        self.resource_tree.bind('<Double-1>',self._resource_row_double_click)
        self.resource_tree.bind('<<TreeviewSelect>>',self._resource_selection_changed)

        detail=ttk.Frame(outer,style='Card.TFrame',padding=(10,6)); detail.pack(fill='x',pady=(4,0))
        self.resource_detail_var=tk.StringVar(value='选择资源行后显示完整 Bundle 名称、推断路径与关联信息。')
        ttk.Label(detail,textvariable=self.resource_detail_var,style='CardMuted.TLabel',wraplength=1200,justify='left').pack(fill='x')

        quick=ttk.Frame(outer,style='Card.TFrame',padding=8); quick.pack(fill='x',pady=(7,0))
        ttk.Label(quick,text='快速选择：',style='Card.TLabel').pack(side='left')
        ttk.Button(quick,text='全选本页',command=self.select_current_resource_page).pack(side='left',padx=(6,0))
        ttk.Button(quick,text='全选筛选结果',command=self.select_all_filtered_resources).pack(side='left',padx=(6,0))
        ttk.Button(quick,text='反选筛选结果',command=self.invert_filtered_resources).pack(side='left',padx=(6,0))
        ttk.Button(quick,text='仅选 L2D',command=lambda:self.quick_select_resource_kinds(('l2d_motion','l2d_core','l2d_fade','Live2D','L2D'))).pack(side='left',padx=(6,0))
        ttk.Button(quick,text='仅选人物立绘',command=lambda:self.quick_select_resource_kinds(('stand','stand_helper','character_cutout','unitcutin'))).pack(side='left',padx=(6,0))
        ttk.Button(quick,text='仅选人物核心资源',command=self.quick_select_character_core).pack(side='left',padx=(6,0))
        ttk.Button(quick,text='清空选择',command=self.clear_resource_selection).pack(side='left',padx=(6,0))

        actions=ttk.Frame(outer,style='Card.TFrame',padding=10); actions.pack(fill='x',pady=(8,0))
        ttk.Label(actions,text='下载与提取方式由“设置 → 下载与提取”统一控制',style='Card.TLabel').grid(row=0,column=0,sticky='w')
        ttk.Button(actions,text='下载选中项',style='Accent.TButton',command=self.download_selected_resources).grid(row=0,column=1,padx=(10,0),sticky='w')
        ttk.Button(actions,text='下载当前筛选全部',command=self.download_filtered_resources).grid(row=0,column=2,padx=(6,0),sticky='w')
        ttk.Label(actions,text='人物特征 ID（9位）',style='Card.TLabel').grid(row=1,column=0,sticky='w',pady=(9,0))
        ttk.Entry(actions,textvariable=self.resource_character_var,width=16).grid(row=1,column=1,sticky='w',pady=(9,0),padx=(10,0))
        ttk.Button(actions,text='下载人物完整关联集',style='Accent.TButton',command=self.download_character_resources).grid(row=1,column=2,sticky='w',padx=(6,0),pady=(9,0))
        ttk.Label(actions,text='人物模式会先下载立绘、关联剧情、Live2D、语音等；能从剧情文本识别的 BG/BGM/SE 会加入“公共资源”。',style='CardMuted.TLabel',wraplength=980).grid(row=2,column=0,columnspan=4,sticky='w',pady=(8,0))
        ttk.Label(actions,textvariable=self.resource_table_summary_var,style='CardMuted.TLabel').grid(row=3,column=0,columnspan=5,sticky='w',pady=(7,0))
        filebar=ttk.Frame(outer,style='Root.TFrame'); filebar.pack(fill='x',pady=(7,0))
        ttk.Button(filebar,text='打开资源表目录',command=self.open_resource_tables).pack(side='left')
        ttk.Button(filebar,text='打开新增资源表',command=self.open_added_resource_table).pack(side='left',padx=(6,0))
        self.selected_resource_count_var=tk.StringVar(value='已选 0 项（可跨页累积）')
        ttk.Label(filebar,textvariable=self.selected_resource_count_var,style='Muted.TLabel').pack(side='right')
        self.resource_query_var.trace_add('write',self._resource_filter_changed)
        self.resource_kind_combo.bind('<<ComboboxSelected>>',lambda _e:self._apply_resource_filter())
        self.resource_scope_combo.bind('<<ComboboxSelected>>',lambda _e:self._apply_resource_filter())

    def _resolve_configured_path(self,value,fallback):
        raw=str(value or '').strip()
        path=Path(raw).expanduser() if raw else Path(fallback)
        if not path.is_absolute(): path=TOOL_DIR/path
        return path.resolve()

    def _io_workers(self):
        try: return max(1,min(64,int(self.download_workers_var.get())))
        except Exception: return IO_WORKERS

    def _choose_path_into(self,var,folder=False,title='选择目录',filetypes=None):
        current=self._resolve_configured_path(var.get(),TOOL_DIR)
        if folder:
            chosen=filedialog.askdirectory(title=title,initialdir=str(current))
        else:
            chosen=filedialog.askopenfilename(title=title,initialdir=str(current.parent),filetypes=filetypes or [('所有文件','*.*')])
        if chosen:
            var.set(chosen)
            self._save_config()

    def open_resource_tables(self):
        path=self._resolve_configured_path(self.resource_table_root_var.get(),TOOL_DIR/'resource_tables')
        path.mkdir(parents=True,exist_ok=True)
        self.open_path(path)

    def open_added_resource_table(self):
        path=self._resolve_configured_path(self.resource_table_root_var.get(),TOOL_DIR/'resource_tables')/'新增资源表.csv'
        if path.is_file():
            try: os.startfile(path)
            except Exception: self.open_path(path.parent)
        else:
            messagebox.showinfo('尚无新增资源表','请先加载或更新 Catalog，程序会自动生成新增资源清单。')

    def select_all_filtered_resources(self):
        self.resource_selected_names.update(r.get('name','') for r in self.resource_filtered_records if r.get('name'))
        self._render_resource_page(); self._resource_selection_changed()

    def invert_filtered_resources(self):
        for row in self.resource_filtered_records:
            name=row.get('name','')
            if not name: continue
            if name in self.resource_selected_names: self.resource_selected_names.discard(name)
            else: self.resource_selected_names.add(name)
        self._render_resource_page(); self._resource_selection_changed()

    def quick_select_resource_kinds(self, needles):
        lowered=tuple(str(x).lower() for x in needles)
        matched=[r for r in self.resource_filtered_records if any(n in str(r.get('kind','')).lower() or n in str(r.get('name','')).lower() for n in lowered)]
        if not matched:
            messagebox.showinfo('没有匹配资源','当前筛选结果中没有匹配的资源类型。请先清空或调整筛选条件。'); return
        self.resource_selected_names.update(r.get('name','') for r in matched if r.get('name'))
        self._render_resource_page(); self._resource_selection_changed()
        self.resource_status_var.set(f'快速选择完成：新增勾选 {len(matched):,} 条匹配资源（可跨页累积）。')

    def open_settings(self):
        if getattr(self,'settings_window',None) is not None and self.settings_window.winfo_exists():
            self.settings_window.deiconify(); self.settings_window.lift(); return
        win=tk.Toplevel(self); self.settings_window=win
        win.title('Dot 离线资源维护器 - 设置')
        win.configure(bg='#0d1424'); win.geometry('920x690'); win.minsize(780,580)
        win.transient(self)
        outer=ttk.Frame(win,style='Root.TFrame',padding=14); outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='设置',style='Title.TLabel').pack(anchor='w')
        ttk.Label(outer,text='路径与 Catalog、下载并发、提取方式在此统一调整；保存后立即用于后续任务。',style='Muted.TLabel').pack(anchor='w',pady=(2,10))
        tabs=ttk.Notebook(outer); tabs.pack(fill='both',expand=True)
        paths=ttk.Frame(tabs,style='Root.TFrame',padding=12); behavior=ttk.Frame(tabs,style='Root.TFrame',padding=12); player=ttk.Frame(tabs,style='Root.TFrame',padding=12)
        tabs.add(paths,text='路径与 Catalog'); tabs.add(behavior,text='下载与提取'); tabs.add(player,text='播放器选项')

        def path_row(parent,row,label,var,kind='folder',filetypes=None):
            ttk.Label(parent,text=label,style='Card.TLabel').grid(row=row,column=0,sticky='w',pady=6)
            ttk.Entry(parent,textvariable=var).grid(row=row,column=1,sticky='ew',padx=(10,8),pady=6)
            if kind=='catalog':
                cmd=lambda:self._choose_path_into(var,folder=False,title='选择 catalog_1.bin',filetypes=[('Catalog','*.bin'),('所有文件','*.*')])
            else:
                cmd=lambda:self._choose_path_into(var,folder=True,title=label)
            ttk.Button(parent,text='浏览…',command=cmd).grid(row=row,column=2,pady=6)
        for frm in (paths,): frm.columnconfigure(1,weight=1)
        path_row(paths,0,'播放器项目根目录',self.project_var)
        path_row(paths,1,'本地 Catalog 文件',self.catalog_var,'catalog')
        path_row(paths,2,'Bundle 下载缓存目录',self.raw_bundle_root_var)
        path_row(paths,3,'人物资源提取目录',self.asset_library_var)
        path_row(paths,4,'资源表输出目录',self.resource_table_root_var)
        ttk.Label(paths,text='完整 catalog_1.bin 下载地址',style='Card.TLabel').grid(row=5,column=0,sticky='w',pady=(14,5))
        ttk.Entry(paths,textvariable=self.catalog_url_var).grid(row=5,column=1,sticky='ew',padx=(10,8),pady=(14,5))
        ttk.Button(paths,text='解析地址',command=lambda:self._parse_catalog_url(show_error=True)).grid(row=5,column=2,pady=(14,5))
        ttk.Button(paths,text='下载并更新 Catalog',command=self.download_catalog_from_url).grid(row=6,column=1,sticky='w',padx=(10,8),pady=(4,4))
        ttk.Label(paths,textvariable=self.catalog_url_status_var,style='Muted.TLabel',wraplength=760).grid(row=7,column=0,columnspan=3,sticky='w',pady=(5,0))

        ttk.Label(behavior,text='下载线程数（1–64）',style='Card.TLabel').grid(row=0,column=0,sticky='w',pady=(4,9))
        ttk.Spinbox(behavior,from_=1,to=64,textvariable=self.download_workers_var,width=8).grid(row=0,column=1,sticky='w',pady=(4,9))
        ttk.Checkbutton(behavior,text='下载后自动按 Bundle 内部原目录提取分类',variable=self.extract_original_var).grid(row=1,column=0,columnspan=2,sticky='w',pady=7)
        ttk.Checkbutton(behavior,text='人物资源提取时追踪并补齐剧情引用的公共背景 / BGM / SE',variable=self.include_common_var).grid(row=2,column=0,columnspan=2,sticky='w',pady=7)
        ttk.Label(behavior,text='资源表固定自动保存：每次 Catalog 更新覆盖当前表，并覆盖生成新增资源表；不保留历史备份。',style='CardMuted.TLabel',wraplength=760).grid(row=3,column=0,columnspan=3,sticky='w',pady=(12,5))
        ttk.Label(behavior,text='资源表字段不包含 Hash 和扩展名。',style='CardMuted.TLabel').grid(row=4,column=0,columnspan=3,sticky='w')

        ttk.Checkbutton(player,text='播放器资源处理后自动更新索引',variable=self.auto_index_var).grid(row=0,column=0,sticky='w',pady=(4,10))
        ttk.Label(player,text='播放器根目录需包含 data_r18_all；独立人物资源提取不依赖此目录。',style='CardMuted.TLabel',wraplength=760).grid(row=1,column=0,sticky='w')

        bottom=ttk.Frame(outer,style='Root.TFrame'); bottom.pack(fill='x',pady=(10,0))
        ttk.Button(bottom,text='保存设置',style='Accent.TButton',command=lambda:self._save_settings_window(win)).pack(side='right')
        ttk.Button(bottom,text='关闭',command=win.destroy).pack(side='right',padx=(0,8))
        self._parse_catalog_url(show_error=False)

    def _save_settings_window(self,win):
        parsed=self._parse_catalog_url(show_error=False)
        if not parsed:
            messagebox.showwarning('Catalog 地址','请输入完整有效的 catalog_1.bin 下载地址，或修正地址后再保存。',parent=win)
            return
        try:
            workers=int(self.download_workers_var.get())
            if not 1<=workers<=64: raise ValueError
        except Exception:
            messagebox.showwarning('线程数无效','下载线程数范围为 1–64。',parent=win); return
        self._save_config()
        self.resource_table_summary_var.set(f'资源表目录：{self._resolve_configured_path(self.resource_table_root_var.get(),TOOL_DIR/"resource_tables")}')
        win.destroy()
        self._append('[设置] 已保存，后续任务将使用新配置。\n','ok')

    def choose_asset_library(self):
        current=Path(self.asset_library_var.get() or 'catalog_asset_library').expanduser()
        if not current.is_absolute(): current=TOOL_DIR/current
        p=filedialog.askdirectory(title='选择资源提取目录',initialdir=str(current))
        if p:
            self.asset_library_var.set(p); self._save_config()

    def open_asset_library(self):
        path=self._resolve_configured_path(self.asset_library_var.get(),TOOL_DIR/'catalog_asset_library')
        path.mkdir(parents=True,exist_ok=True)
        self.open_path(path)

    def _resource_filter_changed(self, *_args):
        if self.resource_filter_job:
            try: self.after_cancel(self.resource_filter_job)
            except Exception: pass
        self.resource_filter_job=self.after(250,self._apply_resource_filter)

    def _reset_resource_filter(self):
        self.resource_query_var.set('')
        self.resource_kind_var.set('全部类型')
        self.resource_scope_var.set('全部范围')
        self._apply_resource_filter()

    def _load_resource_table_on_startup(self):
        table_dir=self._resolve_configured_path(self.resource_table_root_var.get(), TOOL_DIR/'resource_tables')
        # Build an initial baseline only once. Ordinary app starts refresh the current
        # table/download status but do not erase the last Catalog-update delta report.
        self.load_resource_table(save_delta=not (table_dir/'资源表.json').is_file())

    def load_resource_table(self, save_delta=True):
        cat=Path(self.catalog_var.get().strip()).expanduser()
        if not cat.is_absolute(): cat=TOOL_DIR/cat
        if not cat.is_file():
            self.resource_status_var.set(f'找不到 Catalog：{cat}。请打开“设置”，填写完整地址并点击“下载并更新 Catalog”。')
            return
        if getattr(self,'resource_loading',False): return
        self.resource_loading=True
        self.resource_load_button.configure(state='disabled')
        self.resource_status_var.set(f'正在解析 Catalog：{cat.name} …')
        import threading
        table_dir=self._resolve_configured_path(self.resource_table_root_var.get(), TOOL_DIR/'resource_tables')
        bundle_dir=self._resolve_configured_path(self.raw_bundle_root_var.get(), TOOL_DIR/'raw_bundles')
        threading.Thread(target=self._resource_load_worker,args=(cat,table_dir,bundle_dir,bool(save_delta)),daemon=True).start()

    def _resource_load_worker(self,cat,table_dir,bundle_dir,save_delta=True):
        try:
            records=build_catalog_records(cat, cache_dir=bundle_dir)
            saved=save_resource_tables(records,table_dir,cat,update_delta=save_delta)
            self.log_queue.put(('__resource_loaded__',records,None,str(cat),saved))
        except Exception as exc:
            self.log_queue.put(('__resource_loaded__',[],str(exc),str(cat),None))

    def _apply_resource_filter(self):
        self.resource_filter_job=None
        if not hasattr(self,'resource_tree'): return
        self._capture_visible_resource_selection()
        query=self.resource_query_var.get().strip().lower()
        kind=self.resource_kind_var.get()
        scope=self.resource_scope_var.get()
        filtered=[]
        for record in self.resource_records:
            if kind not in ('','全部类型') and record['kind']!=kind: continue
            if scope not in ('','全部范围') and record['scope']!=scope: continue
            if query:
                hay=' '.join(str(record.get(k,'')) for k in ('name','kind','base_kind','scope','family','character_ids','story_ids','source_path','token')).lower()
                if query not in hay: continue
            filtered.append(record)
        self.resource_filtered_records=filtered
        self.resource_page_index=0
        self._render_resource_page()

    def _render_resource_page(self):
        self._capture_visible_resource_selection()
        self._rendering_resource_tree=True
        for item in self.resource_tree.get_children(''):
            self.resource_tree.delete(item)
        self.resource_iid_to_record.clear()
        self.resource_render_index=0
        self.resource_render_generation+=1
        gen=self.resource_render_generation
        total=len(self.resource_filtered_records)
        page_count=max(1,(total+self.resource_page_size-1)//self.resource_page_size)
        self.resource_page_index=max(0,min(self.resource_page_index,page_count-1))
        start=self.resource_page_index*self.resource_page_size
        end=min(start+self.resource_page_size,total)
        self.resource_page_records=self.resource_filtered_records[start:end]
        self.resource_page_var.set(f'第 {self.resource_page_index+1} / {page_count} 页')
        self.resource_status_var.set(f'Catalog 总计 {len(self.resource_records):,} 条；当前筛选 {total:,} 条；本页 {len(self.resource_page_records):,} 条。')
        self._insert_resource_batch(gen)

    def _change_resource_page(self, delta):
        total=len(self.resource_filtered_records)
        page_count=max(1,(total+self.resource_page_size-1)//self.resource_page_size)
        wanted=self.resource_page_index+int(delta)
        if 0<=wanted<page_count:
            self.resource_page_index=wanted
            self._render_resource_page()

    def _insert_resource_batch(self,generation):
        if generation!=self.resource_render_generation: return
        end=min(self.resource_render_index+250,len(self.resource_page_records))
        cols=('name','kind','character_ids','story_ids','downloaded')
        base_idx=self.resource_page_index*self.resource_page_size
        for local_idx in range(self.resource_render_index,end):
            rec=self.resource_page_records[local_idx]
            global_idx=base_idx+local_idx
            iid=f'resource_{global_idx}'
            vals=tuple(rec.get(c,'') for c in cols)
            self.resource_tree.insert('','end',iid=iid,values=vals)
            self.resource_iid_to_record[iid]=rec
            if rec.get('name') in self.resource_selected_names:
                self.resource_tree.selection_add(iid)
        self.resource_render_index=end
        if end<len(self.resource_page_records):
            self.resource_render_after=self.after(1,lambda:self._insert_resource_batch(generation))
        else:
            self._rendering_resource_tree=False
            self.resource_status_var.set(f'Catalog 总计 {len(self.resource_records):,} 条；筛选匹配 {len(self.resource_filtered_records):,} 条；当前第 {self.resource_page_index+1} 页显示 {len(self.resource_page_records):,} 条。')
            self.resource_render_after=None
            self._update_selected_count()

    def _resource_row_double_click(self,event):
        iid=self.resource_tree.identify_row(event.y)
        if not iid: return
        rec=self.resource_iid_to_record.get(iid)
        if rec:
            self.resource_detail_var.set('名称：'+str(rec.get('name',''))+'  |  类型：'+str(rec.get('kind',''))+'  |  路径推断：'+str(rec.get('source_path','')))
            self.resource_tree.selection_set(iid)
            self.resource_tree.focus(iid)

    def _capture_visible_resource_selection(self):
        if not hasattr(self,'resource_tree') or self._rendering_resource_tree:
            return
        current_names={r.get('name','') for r in getattr(self,'resource_page_records',[]) if r.get('name')}
        selected_names=set()
        for iid in self.resource_tree.selection():
            rec=self.resource_iid_to_record.get(iid)
            if rec and rec.get('name'):
                selected_names.add(rec['name'])
        self.resource_selected_names.difference_update(current_names)
        self.resource_selected_names.update(selected_names)
        self._update_selected_count()

    def _resource_selection_changed(self,_event=None):
        if not self._rendering_resource_tree:
            self._capture_visible_resource_selection()
        if hasattr(self,'resource_detail_var'):
            selected=self.resource_tree.selection() if hasattr(self,'resource_tree') else ()
            rec=self.resource_iid_to_record.get(selected[0]) if selected else None
            if rec:
                self.resource_detail_var.set('名称：'+str(rec.get('name',''))+'  |  类型：'+str(rec.get('kind',''))+'  |  范围：'+str(rec.get('scope',''))+'  |  人物ID：'+str(rec.get('character_ids',''))+'  |  剧情ID：'+str(rec.get('story_ids',''))+'  |  路径推断：'+str(rec.get('source_path','')))
            else:
                self.resource_detail_var.set('选择资源行后显示完整 Bundle 名称、推断路径与关联信息。')

    def quick_select_character_core(self):
        """Select only resources useful for the character export pipeline."""
        self._capture_visible_resource_selection()
        wanted=[]
        for rec in self.resource_filtered_records:
            kind=str(rec.get('kind','')).lower()
            name=str(rec.get('name','')).lower()
            if any(token in kind for token in ('l2d','live2d','立绘','stand','spine')) or any(token in name for token in ('charastand','l2d','live2d','general-spine','dotnovelcharaicon')):
                wanted.append(rec.get('name',''))
        self.resource_selected_names.update(x for x in wanted if x)
        self._render_resource_page(); self._update_selected_count()

    def _update_selected_count(self):
        if hasattr(self,'selected_resource_count_var'):
            self.selected_resource_count_var.set(f'已选 {len(self.resource_selected_names):,} 项（可跨页累积）')

    def select_current_resource_page(self):
        self._rendering_resource_tree=True
        for iid,rec in list(self.resource_iid_to_record.items()):
            self.resource_tree.selection_add(iid)
            if rec.get('name'):
                self.resource_selected_names.add(rec['name'])
        self._rendering_resource_tree=False
        self._update_selected_count()

    def clear_resource_selection(self):
        self.resource_selected_names.clear()
        self._rendering_resource_tree=True
        try: self.resource_tree.selection_remove(*self.resource_tree.selection())
        finally: self._rendering_resource_tree=False
        self._update_selected_count()

    def _selected_resource_names(self):
        self._capture_visible_resource_selection()
        if self.resource_selected_names:
            return sorted(self.resource_selected_names)
        records=[]
        for iid in self.resource_tree.selection():
            rec=self.resource_iid_to_record.get(iid)
            if rec: records.append(rec)
        return list(dict.fromkeys(r['name'] for r in records))

    def _run_catalog_resource_job(self,names=None,character_id=''):
        parsed=self._parse_catalog_url(show_error=True)
        if not parsed: return
        cat=Path(self.catalog_var.get().strip()).expanduser()
        if not cat.is_absolute(): cat=TOOL_DIR/cat
        if not cat.is_file():
            messagebox.showwarning('缺少 Catalog',f'找不到：\n{cat}\n请先下载或选择有效的 Catalog。'); return
        output=Path(self.asset_library_var.get().strip() or (TOOL_DIR/'catalog_asset_library')).expanduser()
        if not output.is_absolute(): output=TOOL_DIR/output
        raw=self._resolve_configured_path(self.raw_bundle_root_var.get(), TOOL_DIR/'raw_bundles')
        cmd=self._py('catalog_asset_tool.py','--catalog',cat,'--base-url',parsed['base_url'],'--output',raw,'--export-root',output,'--workers',str(self._io_workers()))
        if character_id:
            cmd += ['--character-id',character_id]
        else:
            if not names: return messagebox.showwarning('没有选择资源','请先选中资源行，或调整筛选条件。')
            runtime_dir().mkdir(parents=True,exist_ok=True)
            name_file=runtime_dir()/'catalog_selected_names.json'
            name_file.write_text(json.dumps({'names':list(names)},ensure_ascii=False,indent=2),encoding='utf-8')
            cmd += ['--names-file',name_file]
        # Character export must use the canonical resource-maintainer pipeline below.
        # Avoid a duplicate generic UnityPy dump (Assets/.../原始目录) which caused
        # bloated output trees and left raw objects unconverted.
        if character_id and self.extract_original_var.get():
            cmd.append('--no-extract')
        elif not self.extract_original_var.get():
            cmd.append('--no-extract')
        if not self.include_common_var.get(): cmd.append('--no-common')
        self._save_config()
        commands=[(cmd,{0})]
        if character_id and self.extract_original_var.get():
            conversion_root=output/character_id
            manifest=raw/'.dot_runtime'/('manifest_character_'+character_id+'.json')
            pipeline_cmd=self._py('run_character_conversion.py','--manifest',manifest,'--raw-dir',raw,'--output-root',conversion_root,'--base-url',parsed['base_url'],'--workers',str(self._io_workers()))
            commands.append((pipeline_cmd,{0}))
        label=(f'人物 {character_id} 关联资源下载、提取与转换' if character_id else f'指定 Bundle 下载 {len(names)} 项')
        self.run_commands(commands,label,callback=lambda:self.load_resource_table(save_delta=False))

    def download_selected_resources(self):
        names=self._selected_resource_names()
        if not names: return messagebox.showwarning('没有选择资源','在资源表中选中一项或多项后再下载。')
        self._run_catalog_resource_job(names=names)

    def download_filtered_resources(self):
        names=[r['name'] for r in self.resource_filtered_records]
        if not names: return messagebox.showwarning('没有匹配资源','请先加载 Catalog 并设置筛选条件。')
        if not messagebox.askyesno('下载筛选结果',f'当前筛选匹配 {len(names):,} 个 Bundle。将逐一下载'+('并按原目录提取。' if self.extract_original_var.get() else '。')+'\n\n继续吗？'):
            return
        self._run_catalog_resource_job(names=names)

    def download_character_resources(self):
        cid=self.resource_character_var.get().strip()
        if not re.fullmatch(r'\d{9}',cid):
            return messagebox.showwarning('人物特征 ID 无效','请输入 9 位数字人物特征 ID，例如 100101000。')
        try:
            matches,story_ids=character_bundle_names([r['name'] for r in self.resource_records],cid)
        except Exception as exc:
            return messagebox.showwarning('人物资源检索失败',str(exc))
        if not matches:
            return messagebox.showinfo('未找到资源',f'Catalog 中没有找到人物 {cid} 的可识别资源。')
        text=f'人物特征 ID：{cid}\n初步匹配 Bundle：{len(matches):,}\n关联剧情 ID：{len(story_ids):,}\n\n'
        text+='将下载人物立绘、剧情、Live2D、语音及可识别的关联资源。若提取后能从剧情文本读出公共 BG/BGM/SE 编号，也会自动追加下载并归档到人物“公共资源”及共享公共池。\n\n继续吗？'
        if not messagebox.askyesno('下载人物完整关联资源',text): return
        self._run_catalog_resource_job(character_id=cid)

    def choose_project(self):
        p=filedialog.askdirectory(title='选择播放器项目根目录',initialdir=self.project_var.get() or str(find_project_root(TOOL_DIR)))
        if p: self.project_var.set(p); self._save_config()

    def choose_catalog(self):
        p=filedialog.askopenfilename(title='选择 catalog_1.bin',filetypes=[('Catalog','*.bin'),('All','*.*')],initialdir=str(Path(self.catalog_var.get()).parent if self.catalog_var.get() else TOOL_DIR))
        if p: self.catalog_var.set(p); self._save_config()

    def _validate(self, need_version=False, require_project=True):
        root=self._resolve_configured_path(self.project_var.get(), TOOL_DIR)
        if not root.exists():
            messagebox.showwarning('目录不存在',f'目录不存在：\n{root}'); return None
        if require_project and not (root/'data_r18_all').exists():
            messagebox.showwarning('项目目录不正确','此操作需要现有播放器项目，项目根目录至少应包含 data_r18_all。工具可放在根目录或其子文件夹中。'); return None
        cat=Path(self.catalog_var.get()).expanduser().resolve()
        if need_version:
            if not cat.is_file(): messagebox.showwarning('缺少 Catalog',f'找不到：\n{cat}'); return None
            if not self.version_var.get().strip().isdigit(): messagebox.showwarning('CDN 版本号错误','请输入纯数字，例如 8374。上方保留了版本号获取指引。'); return None
        self._save_config(); return root

    def _env(self,root):
        e=os.environ.copy(); e['DOT_PROJECT_ROOT']=str(root); e['DOT_CATALOG']=str(Path(self.catalog_var.get()).expanduser().resolve()); e['DOT_IO_WORKERS']=str(self._io_workers()); e['DOT_CPU_WORKERS']=str(CPU_WORKERS); e['PYTHONUTF8']='1'; e['PYTHONIOENCODING']='utf-8'
        if getattr(self,'catalog_base_url',''): e['DOT_CATALOG_BASE_URL']=self.catalog_base_url
        return e

    def _active_base_url(self):
        try:
            parsed=parse_catalog_url(self.catalog_url_var.get())
            self.catalog_base_url=parsed['base_url']
            return parsed['base_url']
        except Exception:
            return ''

    def _ids(self):
        raw=self.ids_text.get('1.0','end').replace('，',',').replace('\n',',').replace(' ',',')
        return [x.strip() for x in raw.split(',') if x.strip()]

    def _py(self,name,*args): return [sys.executable,str(TOOL_DIR/name),*map(str,args)]

    def list_matches(self):
        root=self._validate(True,False); ids=self._ids()
        if not root or not ids: return messagebox.showwarning('缺少 ID','请输入至少一个 ID。')
        version=self.version_var.get().strip(); cat=self.catalog_var.get().strip()
        base=self._active_base_url()
        url_args=['--base-url',base] if base else ['--version',version]
        cmd=self._py('catalog_downloader.py',*ids,*url_args,'--catalog',cat,'--workers',str(self._io_workers()),'--list-only')
        self.run_commands([(cmd,{0,2})],f'并行检索 {len(ids)} 个 ID 的 Catalog 匹配')

    def download_ids(self,process):
        root=self._validate(True,False); ids=self._ids()
        if not root or not ids: return messagebox.showwarning('缺少 ID','请输入至少一个资源/人物/剧情 ID。')
        version=self.version_var.get().strip(); cat=self.catalog_var.get().strip(); raw=self._resolve_configured_path(self.raw_bundle_root_var.get(), TOOL_DIR/'raw_bundles'); cmds=[]
        manifest=runtime_dir()/'gui_download_manifest.json'
        base=self._active_base_url()
        url_args=['--base-url',base] if base else ['--version',version]
        dl=self._py('catalog_downloader.py',*ids,*url_args,'--catalog',cat,'--output',raw,'--manifest',manifest,'--workers',str(self._io_workers()))
        cmds.append((dl,{0}))
        if process:
            pipeline_args=['--base-url',base] if base else ['--version',version]
            cmds.append((self._py('run_pipeline.py','--manifest',manifest,*pipeline_args,'--workers',str(self._io_workers())),{0}))
        if process and self.auto_index_var.get(): cmds.append((self._py('maintenance_tool.py','--root',root,'--action','index'),{0}))
        self.run_commands(cmds,('下载并处理' if process else '仅下载')+f' {len(ids)} 个任务（统一缓存 / 并发 {self._io_workers()}）')

    def process_local(self):
        root=self._validate(True,False)
        if not root: return
        files=filedialog.askopenfilenames(title='选择 Unity Bundle',filetypes=[('Unity Bundle','*.bundle'),('All files','*.*')])
        if not files: return
        base=self._active_base_url()
        url_args=['--base-url',base] if base else ['--version',self.version_var.get().strip()]
        cmd=self._py('run_pipeline.py',*files,*url_args,'--workers',str(self._io_workers()))
        cmds=[(cmd,{0})]
        if self.auto_index_var.get(): cmds.append((self._py('maintenance_tool.py','--root',root,'--action','index'),{0}))
        self.run_commands(cmds,f'处理 {len(files)} 个本地 Bundle（统一缓存）')

    def _load_cached_audit_summary(self):
        try:
            self._refresh_audit(self.latest_audit_path,quiet=True)
        except Exception:
            pass

    def _audit_report_matches_root(self,path,root):
        try:
            d=json.loads(Path(path).read_text(encoding='utf-8-sig'))
            return Path(d.get('root','')).resolve()==Path(root).resolve()
        except Exception:
            return False

    def _invalidate_audit_cache(self,reason='资源结构已发生变化'):
        try:
            if self.latest_audit_path.exists(): self.latest_audit_path.unlink()
        except Exception:
            pass
        self.audit_summary_var.set(f'{reason}；请重新执行一次完整资源检查。')

    def audit_only(self):
        root=self._validate(False)
        if not root: return
        REPORT_DIR.mkdir(exist_ok=True); out=self.latest_audit_path
        self.run_commands([(self._py('resource_audit.py','--root',root,'--workers',str(self._io_workers()),'--output',out),{0,1})],'检查全部剧情资源',callback=lambda:self._refresh_audit(out))

    def audit_and_repair(self):
        root=self._validate(True)
        if not root: return
        REPORT_DIR.mkdir(exist_ok=True); report=self.latest_audit_path
        reuse=report.is_file() and self._audit_report_matches_root(report,root)
        reuse_text='将复用上一次完整检查结果，只定向复核其中有问题的剧情，不会重新扫描全部 story.json。' if reuse else '当前没有可复用的检查报告，将先执行一次完整检查。'
        if not messagebox.askyesno('自动补全缺失资源',reuse_text+'\n\n公共资源会先查现有多路径和公共池；剧情私有资源只下载/处理缺失类型对应的 Bundle。\n\n继续吗？'): return
        cmds=[]
        if not reuse:
            cmds.append((self._py('resource_audit.py','--root',root,'--workers',str(self._io_workers()),'--output',report),{0,1}))
        base=self._active_base_url()
        url_args=['--base-url',base] if base else ['--version',self.version_var.get().strip()]
        cmds.append((self._py('resource_repair.py','--root',root,*url_args,'--report',report,'--output',report,'--workers',str(self._io_workers())),{0,1}))
        title='复用上次检查结果并自动修复' if reuse else '检查并自动补全缺失资源'
        self.run_commands(cmds,title,callback=lambda:self._refresh_audit(report))

    def _refresh_audit(self,path,quiet=False):
        try:
            d=json.loads(Path(path).read_text(encoding='utf-8-sig'))
            root=self._resolve_configured_path(self.project_var.get(), TOOL_DIR)
            if Path(d.get('root','')).resolve()!=root:
                if not quiet: self.audit_summary_var.set('检查报告属于其他项目，不能复用。')
                return False
            kinds='，'.join(f'{k}:{v}' for k,v in sorted((d.get('byKind') or {}).items())) or '无'
            when=str(d.get('generatedAt') or '')
            suffix=f' | {when}' if when else ''
            self.audit_summary_var.set(f"可复用检查结果：剧情 {d.get('stories',0)} | 缺失 {d.get('issueCount',0)} | {kinds}{suffix}")
            return True
        except Exception as exc:
            if not quiet: self.audit_summary_var.set(f'报告读取失败：{exc}')
            return False

    def _catalog_file(self):
        cat=Path(self.catalog_var.get()).expanduser().resolve()
        if not cat.is_file():
            messagebox.showwarning('缺少 Catalog',f'找不到：\n{cat}')
            return None
        return cat

    def full_story_scan(self):
        root=self._validate(False)
        if not root: return
        cat=self._catalog_file()
        if not cat: return
        REPORT_DIR.mkdir(exist_ok=True)
        out=REPORT_DIR/'catalog_story_mapping.json'
        cmd=self._py('full_story_sync.py','--root',root,'--catalog',cat,'--scan-only','--output',out)
        self.run_commands([(cmd,{0,1})],'扫描 Catalog 全量剧情映射',callback=lambda:self._refresh_full_story(out))

    def full_story_repair(self):
        root=self._validate(True)
        if not root: return
        cat=self._catalog_file()
        if not cat: return
        if not messagebox.askyesno(
            '全量补充缺失剧情',
            '将先按“角色 ID → 剧情 ID”建立 Catalog 映射，再与本地 story.json 对比。\n\n'
            '只下载缺失剧情对应的剧本、Voice/BGV、Live2D、缩略图，并检查/补齐人物立绘；已有完整剧情不会重复下载。剧情解析后还会自动恢复 story 中引用的 BG/BGM/SE，并更新全部索引。\n\n'
            '首次执行可能需要下载较多资源，是否继续？'
        ): return
        REPORT_DIR.mkdir(exist_ok=True)
        out=REPORT_DIR/'full_story_supplement.json'
        base=self._active_base_url()
        url_args=['--base-url',base] if base else ['--version',self.version_var.get().strip()]
        cmd=self._py(
            'full_story_sync.py','--root',root,'--catalog',cat,*url_args,
            '--download-workers',str(self._io_workers()),'--output',out
        )
        self.run_commands([(cmd,{0})],'全量补充缺失剧情',callback=lambda:(self._refresh_full_story(out),self._invalidate_audit_cache('已补充新的剧情资源')))

    def _refresh_full_story(self,path):
        try:
            d=json.loads(Path(path).read_text(encoding='utf-8-sig'))
            st=d.get('stats') or {}
            self.full_story_summary_var.set(
                f"角色 {st.get('characters',0)} | Catalog 剧情 {st.get('catalogStories',0)} | "
                f"缺剧情 {st.get('missingStories',0)} | 缺立绘 {st.get('missingStandVariants',0)} | 缺缩略图 {st.get('missingThumbs',0)}"
            )
        except Exception as exc:
            self.full_story_summary_var.set(f'报告读取失败：{exc}')

    def maintenance(self,action):
        root=self._validate(False)
        if not root: return
        if action=='pool_only' and not messagebox.askyesno('仅保留公共池','此操作会删除与公共池 SHA-256 完全相同的旧 BGM/SE 副本，并清理空目录；冲突文件不会删除。完成后会强制同步索引。\n\n继续吗？'): return
        cmd=self._py('maintenance_tool.py','--root',root,'--action',action)
        self.run_commands([(cmd,{0})],{'index':'更新全部索引','report':'扫描重复资源','pool':'建立公共池','link':'硬链接去重','pool_only':'仅保留公共池'}[action])

    def run_commands(self,commands,title,callback=None):
        if self.running: return messagebox.showinfo('任务正在运行','请等待当前任务结束，或在“运行日志”页停止任务。')
        selected=self.workspace_tabs.select()
        self.job_workspace='assets' if selected==str(self.extract_workspace) else 'player'
        self.running=True; self.status_var.set(title); self.busy.start(12)
        if self.job_workspace=='assets': self.asset_tabs.select(self.asset_log_tab)
        else: self.player_tabs.select(self.log_tab)
        self.progress_value.set(0); self.progress_percent_var.set('0%')
        self.progress_stage_var.set(title); self.progress_detail_var.set('正在启动任务…')
        self._append(f'\n=== {title} ===\n','info')
        root=self._resolve_configured_path(self.project_var.get(), TOOL_DIR)
        if not root.exists(): root=TOOL_DIR
        env=self._env(root)
        threading.Thread(target=self._worker,args=(commands,root,env,callback,title),daemon=True).start()

    def _worker(self,commands,root,env,callback,title):
        overall=0
        total_cmds=max(1,len(commands))
        try:
            for ci,(cmd,ok_codes) in enumerate(commands,1):
                coarse_start=(ci-1)*100/total_cmds
                self.log_queue.put(('__progress__',{'percent':coarse_start,'stage':title,'detail':f'执行步骤 {ci}/{total_cmds}'}))
                self.log_queue.put(('info','> '+subprocess.list2cmdline(cmd)+'\n'))
                flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
                child_env=dict(env); child_env['PYTHONUNBUFFERED']='1'
                self.proc=subprocess.Popen(cmd,cwd=root,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',bufsize=1,creationflags=flags,env=child_env)
                assert self.proc.stdout is not None
                for line in self.proc.stdout:
                    if line.startswith('@@DOT_PROGRESS\t'):
                        try:
                            payload=json.loads(line.split('\t',1)[1])
                            self.log_queue.put(('__progress__',payload))
                            continue
                        except Exception:
                            pass
                    up=line.upper(); tag='error' if ('[FAIL' in up or '[ERROR' in up or 'TRACEBACK' in up) else ('ok' if ('[OK]' in up or '[DONE]' in up or 'COMPLETE' in up or '[AUDIT AFTER]' in up) else '')
                    self.log_queue.put((tag,line))
                code=self.proc.wait(); self.proc=None
                if code not in ok_codes:
                    overall=code or 1; break
                self.log_queue.put(('__progress__',{'percent':ci*100/total_cmds,'stage':title,'detail':f'步骤 {ci}/{total_cmds} 完成'}))
        except Exception as exc:
            overall=1; self.log_queue.put(('error',f'[ERROR] {exc}\n'))
        self.log_queue.put(('__done__',overall,callback))

    def stop_task(self):
        if self.proc:
            try: self.proc.terminate(); self._append('[INFO] 已请求停止当前子进程。\n','info')
            except Exception as exc: self._append(f'[ERROR] {exc}\n','error')

    def _clear_log(self, target='log'):
        widget=getattr(self,target,None)
        if widget is not None:
            widget.delete('1.0','end')

    def _append(self,text,tag=''):
        target='asset_log' if getattr(self,'job_workspace','player')=='assets' else 'log'
        widget=getattr(self,target,None) or getattr(self,'log',None)
        if widget is None:
            return
        widget.insert('end',text,tag if tag else None)
        widget.see('end')

    def _drain(self):
        self._drain_after_id=None
        try:
            while True:
                item=self.log_queue.get_nowait()
                if item[0]=='__done__':
                    code,cb=item[1],item[2]; self.running=False; self.busy.stop(); self.status_var.set('完成' if code==0 else f'结束（代码 {code}）')
                    if code==0:
                        self.progress_value.set(100); self.progress_percent_var.set('100%'); self.progress_stage_var.set('完成')
                        if self.progress_detail_var.get() in ('正在启动任务…',''):
                            self.progress_detail_var.set('任务已完成。')
                    else:
                        self.progress_stage_var.set(f'结束（代码 {code}）')
                    self._append('=== 完成 ===\n' if code==0 else f'=== 结束：{code} ===\n','ok' if code==0 else 'error')
                    if cb and code==0:
                        try: cb()
                        except Exception as exc: self._append(f'[WARN] 完成回调失败: {exc}\n','error')
                    elif cb and code!=0:
                        self._append('[WARN] 任务未成功结束，已跳过完成回调，未刷新资源表。\n','error')
                elif item[0]=='__resource_loaded__':
                    self.resource_loading=False
                    try: self.resource_load_button.configure(state='normal')
                    except Exception: pass
                    records=item[1] or []; err=item[2]; catalog_name=item[3]; saved=item[4] if len(item)>4 else None
                    if err:
                        self.resource_status_var.set(f'Catalog 解析失败：{err}')
                        self._append(f'[ERROR] Catalog 解析失败：{err}\n','error')
                    else:
                        self.resource_records=records
                        self.resource_selected_names.intersection_update({r.get('name','') for r in records})
                        kinds=sorted({r.get('kind','') for r in records if r.get('kind')})
                        if hasattr(self,'resource_kind_combo'):
                            self.resource_kind_combo.configure(values=['全部类型']+kinds)
                        if self.resource_kind_var.get() not in (['全部类型']+kinds): self.resource_kind_var.set('全部类型')
                        self._apply_resource_filter()
                        if saved:
                            if saved.get('delta_updated'):
                                desc=('首次建表' if saved.get('initial') else '本次新增')
                                self.resource_table_summary_var.set(f"资源表已自动覆盖保存：{saved['count']:,} 条；{desc} {saved['added_count']:,} 条。目录：{saved['table_dir']}")
                            else:
                                self.resource_table_summary_var.set(f"资源表已自动覆盖保存：{saved['count']:,} 条；保留上次新增资源清单。目录：{saved['table_dir']}")
                        self._append(f'[CATALOG TABLE] {catalog_name}: {len(records):,} 条资源记录\n','info')
                elif item[0]=='__progress__':
                    payload=item[1] or {}
                    try: pct=max(0.0,min(100.0,float(payload.get('percent',0))))
                    except Exception: pct=0.0
                    self.progress_value.set(pct); self.progress_percent_var.set(f'{pct:.0f}%')
                    stage=str(payload.get('stage') or '处理中'); detail=str(payload.get('detail') or '')
                    cur=payload.get('current'); total=payload.get('total')
                    if cur is not None and total is not None:
                        detail=(detail+'  ' if detail else '')+f'[{cur}/{total}]'
                    self.progress_stage_var.set(stage); self.progress_detail_var.set(detail or '处理中…')
                else: self._append(item[1],item[0])
        except queue.Empty: pass
        try:
            if self.winfo_exists(): self._drain_after_id=self.after(80,self._drain)
        except tk.TclError:
            self._drain_after_id=None

    def open_path(self,path):
        p=Path(path); p.mkdir(parents=True,exist_ok=True) if p==REPORT_DIR else None
        try: os.startfile(p)
        except Exception: subprocess.Popen(['explorer',str(p)])

    def _close(self):
        self._save_config()
        for attr in ('_drain_after_id','_audit_after_id','_startup_resource_load_id','resource_filter_job','resource_render_after'):
            handle=getattr(self,attr,None)
            if handle:
                try: self.after_cancel(handle)
                except Exception: pass
                setattr(self,attr,None)
        self.destroy()

if __name__=='__main__':
    App().mainloop()
