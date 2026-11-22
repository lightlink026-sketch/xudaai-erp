# -*- coding: utf-8 -*-
"""Yupoo album downloader and quotation workbook/PDF generator."""
from __future__ import annotations
import json, os, queue, re, threading, time, traceback, webbrowser, ssl, hashlib, shutil, subprocess, tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs
from urllib.request import Request, urlopen
import sys
from ca_bundle import CA_PEM
from source_records import record_source
from task_center import create_task, update_task
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from io import BytesIO
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
from openpyxl.utils import get_column_letter
from PIL import Image as PILImage
import qrcode
from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Image as PDFImage, PageBreak
from reportlab.lib.units import mm

APP_TITLE = 'Yupoo 相册下载与报价表生成器'
HEADERS = ['编号/num', '尺码/size', '单价/price(RMB)', '单价/price (USD)', '最小起定量/MOQ', '箱规/Carton dimensions', '单箱体积/CBM', '单箱毛重/Gross weight kg', '单箱净重/Net weight kg', '备注/remark', '图片/picture', '二维码看详情 /QR code for details']
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36'

class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.anchors=[]; self.cur=None
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag=='a': self.cur={'href':a.get('href',''),'title':a.get('title',''),'text':'','imgs':[]}
        elif tag=='img' and self.cur is not None:
            src=a.get('src') or a.get('data-src') or a.get('data-original')
            if src: self.cur['imgs'].append(src)
    def handle_data(self,data):
        if self.cur is not None: self.cur['text'] += data
    def handle_endtag(self,tag):
        if tag=='a' and self.cur is not None:
            self.anchors.append(self.cur); self.cur=None

def make_ssl_context():
    errors=[]
    bundle_root=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent))
    cert_paths=[Path(sys.executable).resolve().parent/'cacert.pem',Path(__file__).resolve().parent/'cacert.pem',bundle_root/'certifi'/'cacert.pem']
    seen=set()
    for cafile in cert_paths:
        if str(cafile).casefold() in seen:continue
        seen.add(str(cafile).casefold())
        try:
            if not cafile.is_file():raise FileNotFoundError(f'证书文件不存在：{cafile}')
            ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);ctx.check_hostname=True;ctx.verify_mode=ssl.CERT_REQUIRED
            ctx.load_verify_locations(cafile=str(cafile))
            return ctx
        except Exception as e:errors.append(f'CA 文件 {cafile}: {type(e).__name__}: {e}')
    try:
        # Embedded data remains as a fallback for a standalone EXE without the sidecar file.
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);ctx.check_hostname=True;ctx.verify_mode=ssl.CERT_REQUIRED
        ctx.load_verify_locations(cadata=CA_PEM)
        return ctx
    except Exception as e:errors.append(f'内置 CA 证书: {type(e).__name__}: {e}')
    if hasattr(ssl,'enum_certificates'):
        try:
            certs=ssl.enum_certificates('ROOT')
            pem=''.join(ssl.DER_cert_to_PEM_cert(cert) for cert,encoding,_trust in certs if encoding=='x509_asn')
            if not pem:raise RuntimeError('Windows 系统证书存储中未找到根证书。')
            ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);ctx.check_hostname=True;ctx.verify_mode=ssl.CERT_REQUIRED;ctx.load_verify_locations(cadata=pem)
            return ctx
        except Exception as e:errors.append(f'Windows 根证书: {type(e).__name__}: {e}')
    raise RuntimeError('无法初始化 HTTPS 证书验证：'+'；'.join(errors))

def http_get(url, referer=None, timeout=30):
    headers={'User-Agent':UA,'Accept':'text/html,application/xhtml+xml,image/avif,image/webp,image/*,*/*;q=0.8'}
    curl=shutil.which('curl.exe') or shutil.which('curl')
    curl_error=''
    if curl:
        args=[curl,'--location','--fail','--silent','--show-error','--max-time',str(timeout),'--user-agent',UA,'-H',f'Accept: {headers["Accept"]}']
        if referer:args.extend(['-H',f'Referer: {referer}'])
        args.extend(['--write-out','\n__YUPPOO_META__%{http_code}\t%{content_type}',url])
        try:
            result=subprocess.run(args,capture_output=True,timeout=timeout+5,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            marker=b'\n__YUPPOO_META__';offset=result.stdout.rfind(marker)
            if result.returncode==0 and offset>=0:
                body=result.stdout[:offset];meta=result.stdout[offset+len(marker):].decode('ascii','replace').strip();parts=meta.split('\t',1)
                if parts and parts[0].startswith('2'):return body,parts[1] if len(parts)>1 else ''
            curl_error=(result.stderr or b'').decode('utf-8','replace').strip() or f'curl exit {result.returncode}'
        except Exception as e:curl_error=f'{type(e).__name__}: {e}'
    if referer: headers['Referer']=referer
    req=Request(url,headers=headers)
    try:
        with urlopen(req,timeout=timeout,context=make_ssl_context()) as r:return r.read(),r.headers.get('Content-Type','')
    except Exception as e:
        if curl_error:raise RuntimeError(f'Windows curl/Schannel 失败：{curl_error}；Python HTTPS 后备失败：{type(e).__name__}: {e}') from e
        raise

def safe_name(s):
    s=re.sub(r'[\\/:*?"<>|\x00-\x1f]','_',s).strip(' ._')
    return (s[:100] or '未命名相册')

def discover_albums(start_url, log):
    start_url=start_url.strip().replace(chr(92),'/')
    start_url=re.sub(r'^(https?):/+',lambda m:m.group(1)+'://',start_url,flags=re.I).rstrip('/')
    parsed=urlparse(start_url)
    if parsed.scheme not in ('http','https') or not parsed.netloc: raise ValueError('请输入完整的 http(s) 类目链接。')
    base=f'{parsed.scheme}://{parsed.netloc}'
    pending=[start_url]; visited=set(); albums={}; category_count=0
    while pending and len(visited)<300:
        page=pending.pop(0)
        if page in visited: continue
        visited.add(page); category_count+=1
        log(f'读取类目页：{page}')
        body,_=http_get(page); html=body.decode('utf-8','replace')
        parser=PageParser(); parser.feed(html)
        for a in parser.anchors:
            href=urljoin(page,a['href'])
            m=re.search(r'/albums/(\d+)',urlparse(href).path)
            if m:
                key=m.group(1); cover=''
                for src in a['imgs']:
                    if 'photo.yupoo.com' in src and not re.search(r'logo|icon',src,re.I): cover=urljoin(page,src); break
                title=(a['title'] or a['text']).strip()
                if not title: title=f'相册_{key}'
                albums.setdefault(key,{'id':key,'url':href,'title':title,'cover_url':cover})
            elif '/categories' in urlparse(href).path:
                if urlparse(href).netloc==parsed.netloc and href not in visited and href not in pending: pending.append(href)
            elif re.search(r'[?&]page=\d+',href) and urlparse(href).netloc==parsed.netloc:
                if href not in visited and href not in pending: pending.append(href)
    if not albums: raise RuntimeError('没有在该页面找到相册链接。确认链接可公开访问，且页面显示相册卡片。')
    log(f'发现 {len(albums)} 个相册（扫描 {category_count} 个类目/分页）。')
    return list(albums.values())

def get_album_images(album):
    body,_=http_get(album['url'],album['url']); html=body.decode('utf-8','replace')
    urls=[]
    for m in re.findall(r'https?://photo\.yupoo\.com/[^"\'<>\s]+',html):
        m=m.replace('&amp;','&')
        if re.search(r'\.(?:jpe?g|png|webp)(?:\?|$)',m,re.I) and not re.search(r'/icons/|logo|avatar',m,re.I): urls.append(m)
    # Prefer original-size image URLs, deduplicated by Yupoo photo path before variant.
    result=[]; seen=set()
    for u in urls:
        p=urlparse(u); path=p.path
        base=re.sub(r'/(?:small|medium|square|thumb)(?=\.)','/big',path,flags=re.I)
        if not re.search(r'/big\.',base,re.I):
            base=re.sub(r'/[^/]+\.(jpe?g|png|webp)$',r'/big.\1',base,flags=re.I)
        final=p._replace(path=base).geturl()
        key=(p.netloc.casefold()+'/'+path.rsplit('/',1)[0]).casefold()
        if key not in seen: seen.add(key); result.append(final)
    if not result:
        og=re.search(r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)',html,re.I)
        if og: result=[og.group(1)]
    return result

def download_one(url, path, referer):
    data,_=http_get(url,referer)
    Path(path).write_bytes(data)


def publish_temp_file(temp_path,target_path):
    temp_path=Path(temp_path);target_path=Path(target_path)
    try:
        os.replace(temp_path,target_path);return target_path
    except PermissionError:
        stamp=time.strftime('%Y%m%d_%H%M%S')
        candidate=target_path.with_name(f'{target_path.stem}_{stamp}{target_path.suffix}')
        n=2
        while candidate.exists():
            candidate=target_path.with_name(f'{target_path.stem}_{stamp}_{n}{target_path.suffix}');n+=1
        os.replace(temp_path,candidate);return candidate

DETAIL_FIELDS=['尺码/size','单价/price(RMB)','单价/price (USD)','最小起定量/MOQ','箱规/Carton dimensions','单箱体积/CBM','单箱毛重/Gross weight kg','单箱净重/Net weight kg','备注/remark']
SOURCE_URL_COL=len(HEADERS)+1
DOWNLOAD_LOG_NAME='下载总记录.xlsx'
DOWNLOAD_LOG_HEADERS=['供货商/account','链接/url','产品数量','最近检查时间','下载状态','本地目录','相册ID校验']
FIELD_HEADER_ALIASES={
    '尺码/size':('尺码/size',),
    '单价/price(RMB)':('单价/price(RMB)','单价/price (RMB)','单价/price（RMB）','单价/price'),
    '单价/price (USD)':('单价/price (USD)','单价/price(USD)','单价/price（USD）'),
    '最小起定量/MOQ':('最小起定量/MOQ',),
    '箱规/Carton dimensions':('箱规/Carton dimensions','箱规','外箱尺寸','包装尺寸','Carton dimensions','一箱箱规/cbm'),
    '单箱体积/CBM':('单箱体积/CBM','单箱体积','CBM/CTN'),
    '单箱毛重/Gross weight kg':('单箱毛重/Gross weight kg','单箱毛重','毛重/箱','Gross weight kg'),
    '单箱净重/Net weight kg':('单箱净重/Net weight kg','单箱净重','净重/箱','Net weight kg'),
    '备注/remark':('备注/remark',),
}

def normalize_header(value):
    return re.sub(r'[\s\u3000（）()_-]','',str(value or '')).replace('／','/').casefold()

def get_exchange_rate():
    """Return the latest published CNY-to-USD mid-market rate and its date."""
    url='https://api.frankfurter.dev/v2/rate/cny/usd'
    body,_=http_get(url,timeout=20);data=json.loads(body.decode('utf-8'))
    if str(data.get('base','')).upper()!='CNY' or str(data.get('quote','')).upper()!='USD':raise RuntimeError('汇率服务返回了非 CNY/USD 数据。')
    rate=Decimal(str(data.get('rate','')))
    if rate<=0:raise RuntimeError('汇率服务返回的 CNY/USD 汇率无效。')
    return rate,str(data.get('date',''))

def parse_price(value):
    if value is None or str(value).strip()=='':return None
    text=str(value).strip().replace(',','').replace('￥','').replace('¥','').replace('RMB','').replace('CNY','').replace('元','').strip()
    try:
        amount=Decimal(text)
        if not amount.is_finite():raise InvalidOperation
        return amount
    except InvalidOperation as e:raise ValueError(f'无法识别人民币单价：{value}') from e

def convert_rmb_to_usd(value,rate):
    amount=parse_price(value)
    if amount is None:return None
    return (amount*rate).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)

def apply_exchange_rate_to_albums(albums,log=None):
    priced=[a for a in albums if parse_price((a.get('details') or {}).get('单价/price(RMB)')) is not None]
    if not priced:return None
    rate,rate_date=get_exchange_rate()
    for album in priced:
        details=album.setdefault('details',{})
        details['单价/price (USD)']=float(convert_rmb_to_usd(details.get('单价/price(RMB)'),rate))
    if log:log(f'汇率已更新：1 CNY = {rate} USD（{rate_date}，Frankfurter 中间价）。')
    return rate,rate_date

def workbook_field_columns(ws):
    aliases={normalize_header(alias):field for field,values in FIELD_HEADER_ALIASES.items() for alias in values}
    columns={};source_url_col=None
    for col in range(1,ws.max_column+1):
        raw=ws.cell(1,col).value
        key=normalize_header(raw)
        if key in aliases:columns[aliases[key]]=col
        if key=='sourceurl':source_url_col=col
    return columns,source_url_col

def save_workbook_safely(wb,path):
    path=Path(path);temp=path.with_name(f'.{path.stem}.{os.getpid()}.{time.time_ns()}.tmp{path.suffix}')
    wb.save(temp);return publish_temp_file(temp,path)

def load_download_log(path):
    path=Path(path)
    records={}
    if not path.exists():return records
    try:
        ws=load_workbook(path,data_only=True).active
        headers={str(c.value):c.column for c in ws[1] if c.value}
        for row in range(2,ws.max_row+1):
            account=str(ws.cell(row,headers.get(DOWNLOAD_LOG_HEADERS[0],1)).value or '').strip()
            url=str(ws.cell(row,headers.get(DOWNLOAD_LOG_HEADERS[1],2)).value or '').strip()
            if not account or not url:continue
            records[(account,url)]={h:ws.cell(row,headers.get(h,i+1)).value for i,h in enumerate(DOWNLOAD_LOG_HEADERS)}
    except Exception:pass
    return records

def save_download_log(path,records):
    path=Path(path);wb=Workbook();ws=wb.active;ws.title='下载总记录';ws.append(DOWNLOAD_LOG_HEADERS)
    navy='1F4E78';thin=Side(style='thin',color='AAB8C7')
    for c in ws[1]:c.fill=PatternFill('solid',fgColor=navy);c.font=Font(name='Microsoft YaHei',size=11,bold=True,color='FFFFFF');c.alignment=Alignment(horizontal='center',vertical='center',wrap_text=True);c.border=Border(left=thin,right=thin,top=thin,bottom=thin)
    for key in sorted(records):
        rec=records[key];ws.append([rec.get(h,'') for h in DOWNLOAD_LOG_HEADERS])
        for c in ws[ws.max_row]:c.alignment=Alignment(vertical='center',wrap_text=True);c.border=Border(left=thin,right=thin,top=thin,bottom=thin)
    for col,width in {'A':22,'B':65,'C':14,'D':24,'E':25,'F':60,'G':20}.items():ws.column_dimensions[col].width=width
    ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions;ws.sheet_view.showGridLines=False
    temp=path.with_name(f'.{path.stem}.{os.getpid()}.{time.time_ns()}.tmp{path.suffix}');wb.save(temp);return publish_temp_file(temp,path)

def album_files_complete(root,album_id):
    root=Path(root)
    for folder in root.iterdir():
        if not folder.is_dir() or not (folder/'info.json').exists():continue
        try:
            info=json.loads((folder/'info.json').read_text(encoding='utf-8'))
            if re.search(r'/albums/(\d+)',info.get('url','')) is None or re.search(r'/albums/(\d+)',info.get('url','')).group(1)!=str(album_id):continue
            paths=info.get('images') or [];expected=int(info.get('expected_image_count',-1))
            return expected>0 and expected==int(info.get('image_count',-1))==len(paths) and all(Path(x).is_file() for x in paths) and bool(info.get('cover_path')) and Path(info['cover_path']).is_file()
        except Exception:return False
    return False

def read_product_detail(folder):
    path=Path(folder)/'商品详情.txt';result={}
    if not path.exists():return result
    alias_to_field={normalize_header(alias):field for field,aliases in FIELD_HEADER_ALIASES.items() for alias in aliases}
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        if '：' in line:
            key,value=line.split('：',1);key=key.strip();result[alias_to_field.get(normalize_header(key),key)]=value.strip()
    return result

def write_product_detail(folder,title,url,values=None):
    folder=Path(folder);path=folder/'商品详情.txt';old=read_product_detail(folder)
    merged={k:str((values or {}).get(k) or '').strip() for k in DETAIL_FIELDS}
    # Existing non-empty values are treated as human-maintained data and win
    # over stale workbook values when a supplier album is refreshed.
    for key in DETAIL_FIELDS:
        if str(old.get(key) or '').strip():merged[key]=str(old[key]).strip()
    known={normalize_header(alias) for aliases in FIELD_HEADER_ALIASES.values() for alias in aliases}
    known.update({normalize_header('商品名称'),normalize_header('相册链接')})
    extras=[]
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if '：' in line and normalize_header(line.split('：',1)[0]) in known:continue
            extras.append(line)
    lines=[f'商品名称：{title}',f'相册链接：{url}']+[f'{key}：{merged.get(key,"")}' for key in DETAIL_FIELDS]+extras
    path.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return merged

def sync_excel_to_txt(root,log,excel_path=None):
    root=Path(root);book=Path(excel_path) if excel_path else root/(f'{root.name}_商品报价表.xlsx')
    if not book.exists() and not excel_path:book=root/'Yupoo_商品报价表.xlsx'
    if not book.exists(): raise RuntimeError(f'找不到 Excel：{book}。请先生成或放入该账号的报价表。')
    wb=load_workbook(book,data_only=False);ws=wb.active;columns,url_col=workbook_field_columns(ws)
    if not url_col:url_col=9 if ws.max_column<=9 else SOURCE_URL_COL
    folders=[d for d in root.iterdir() if d.is_dir() and (d/'info.json').exists()]
    by_id={}
    by_title={}
    for folder in folders:
        try: info=json.loads((folder/'info.json').read_text(encoding='utf-8'))
        except Exception: continue
        m=re.search(r'/albums/(\d+)',info.get('url',''))
        if m:by_id[m.group(1)]=(folder,info)
        by_title[info.get('title','').strip()]=(folder,info)
    count=0;unmatched=[];rows=[]
    for r in range(2,ws.max_row+1):
        title=str(ws.cell(r,1).value or '').strip()
        url=str(ws.cell(r,url_col).value or '').strip() if ws.max_column>=url_col else ''
        m=re.search(r'/albums/(\d+)',url)
        found=by_id.get(m.group(1)) if m else None
        if not found:found=by_title.get(title)
        if not found:
            if any(ws.cell(r,c).value not in (None,'') for c in columns.values()):unmatched.append(title or f'第{r}行')
            continue
        folder,info=found
        values={key:ws.cell(r,col).value for key,col in columns.items()}
        rows.append((r,title,url,folder,info,values))
    has_rmb=any(parse_price(values.get('单价/price(RMB)')) is not None for *_,values in rows)
    fx_rate=fx_date=None
    if has_rmb:
        fx_rate,fx_date=get_exchange_rate()
        usd_col=columns.get('单价/price (USD)')
        if usd_col:
            for r,*_,values in rows:
                amount=convert_rmb_to_usd(values.get('单价/price(RMB)'),fx_rate)
                if amount is not None:
                    ws.cell(r,usd_col,float(amount));ws.cell(r,usd_col).number_format='0.00';values['单价/price (USD)']=float(amount)
    for r,title,url,folder,info,values in rows:
        if values.get('单价/price(RMB)') not in (None,'') and fx_rate is not None:
            amount=convert_rmb_to_usd(values.get('单价/price(RMB)'),fx_rate)
            if amount is not None:values['单价/price (USD)']=f'{amount:.2f}'
        merged=write_product_detail(folder,info.get('title',title),info.get('url',url),values);count+=1
        missing=[label for key,label in (('单价/price(RMB)','采购单价'),('箱规/Carton dimensions','箱规尺寸'),('单箱毛重/Gross weight kg','单箱毛重')) if not str(merged.get(key) or '').strip()]
        record_source('Yupoo',root.name,info.get('title',title),str(folder),info.get('url',url),'待补资料' if missing else '待复核',missing)
    saved_book=book
    if has_rmb and columns.get('单价/price (USD)'):
        saved_book=save_workbook_safely(wb,book);log(f'已按汇率 {fx_rate}（{fx_date}）回写 USD 单价：{saved_book}')
    elif has_rmb:log('警告：当前 Excel 没有“单价/price (USD)”表头，人民币价格已写入 TXT，未能将美元价格回写到 Excel。')
    if unmatched:log(f'警告：有 {len(unmatched)} 行无法对应相册，未回写：{", ".join(unmatched[:5])}')
    log(f'已将 {count} 行 Excel 商品字段回写到对应文件夹的 商品详情.txt。')
    return count,saved_book

def write_excel(albums, root, out):
    wb=Workbook(); ws=wb.active; ws.title='报价表'; ws.append(HEADERS+['source_url'])
    navy='1F4E78'; thin=Side(style='thin',color='AAB8C7')
    widths=[22,14,19,19,22,24,16,22,22,24,53,24,2]
    for i,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=w
    for c in ws[1]:
        c.fill=PatternFill('solid',fgColor=navy); c.font=Font(name='Microsoft YaHei',size=12,bold=True,color='FFFFFF'); c.alignment=Alignment(horizontal='center',vertical='center',wrap_text=True); c.border=Border(left=thin,right=thin,top=thin,bottom=thin)
    ws.row_dimensions[1].height=32
    for idx,a in enumerate(albums,2):
        details=a.get('details') or {}
        ws.cell(idx,1,a['title']);
        for c,key in enumerate(DETAIL_FIELDS,2):ws.cell(idx,c,details.get(key,''))
        ws.cell(idx,3).number_format='0.00';ws.cell(idx,4).number_format='0.00'
        ws.cell(idx,SOURCE_URL_COL,a.get('url',''))
        for c in ws[idx][:len(DETAIL_FIELDS)+1]: c.alignment=Alignment(horizontal='center',vertical='center',wrap_text=True); c.border=Border(left=thin,right=thin,top=thin,bottom=thin)
        cover=a.get('cover_path')
        if cover and Path(cover).exists():
            im=XLImage(cover); ow,oh=im.width,im.height; im.width=375; im.height=375*oh/ow; ws.add_image(im,f'{get_column_letter(len(DETAIL_FIELDS)+2)}{idx}'); ws.row_dimensions[idx].height=im.height*0.75
        picture_col=len(DETAIL_FIELDS)+2;qr_col=picture_col+1
        ws.cell(idx,picture_col).border=Border(left=thin,right=thin,top=thin,bottom=thin); ws.cell(idx,qr_col).border=Border(left=thin,right=thin,top=thin,bottom=thin);ws.cell(idx,SOURCE_URL_COL).border=Border(left=thin,right=thin,top=thin,bottom=thin)
        qr_path=a.get('qr_path')
        if qr_path and Path(qr_path).exists():
            qr=XLImage(qr_path); qr.width=140; qr.height=140; ws.add_image(qr,f'{get_column_letter(qr_col)}{idx}')
    ws.column_dimensions[get_column_letter(SOURCE_URL_COL)].hidden=True;ws.freeze_panes='A2';ws.sheet_view.showGridLines=False
    ws.page_setup.orientation='landscape'; ws.page_setup.paperSize=ws.PAPERSIZE_A3; ws.page_setup.fitToWidth=1;ws.page_setup.fitToHeight=0;ws.sheet_properties.pageSetUpPr.fitToPage=True
    target=Path(out); temp=target.with_name(f'.{target.stem}.{os.getpid()}.{time.time_ns()}.tmp{target.suffix}')
    wb.save(temp); return publish_temp_file(temp,target)

def write_pdf(albums, out):
    font_candidates=[Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/'msyh.ttc',Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/'simhei.ttf']
    font=next((str(p) for p in font_candidates if p.exists()),None)
    if font:
        try: pdfmetrics.registerFont(TTFont('YaHei',font,subfontIndex=0 if font.endswith('.ttc') else 0)); fname='YaHei'
        except Exception: fname='Helvetica'
    else: fname='Helvetica'
    header=ParagraphStyle('header',fontName=fname,fontSize=16,leading=20,alignment=TA_CENTER,textColor=colors.white,wordWrap='CJK')
    body=ParagraphStyle('body',fontName=fname,fontSize=14,leading=19,alignment=TA_CENTER,wordWrap='CJK')
    target=Path(out); temp=target.with_name(f'.{target.stem}.{os.getpid()}.{time.time_ns()}.tmp{target.suffix}')
    doc=SimpleDocTemplate(str(temp),pagesize=landscape(A3),leftMargin=5*mm,rightMargin=5*mm,topMargin=6*mm,bottomMargin=6*mm,title='Yupoo 商品报价表')
    story=[]
    def fitted_image(path,max_width,max_height):
        with PILImage.open(path) as source:width,height=source.size
        scale=min(max_width/width,max_height/height)
        return PDFImage(path,width=width*scale,height=height*scale,hAlign='CENTER')
    for start in range(0,len(albums),2):
        data=[[Paragraph(x,header) for x in HEADERS]]
        for a in albums[start:start+2]:
            details=a.get('details') or {}; cells=[Paragraph(a.get('title',''),body)]
            for key in DETAIL_FIELDS:
                value=details.get(key,''); text='' if value in (None,'') else str(value); text=text.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;'); cells.append(Paragraph(text,body))
            cells.extend([fitted_image(a['cover_path'],220,300) if a.get('cover_path') else Paragraph('',body),fitted_image(a['qr_path'],72,72) if a.get('qr_path') and Path(a['qr_path']).exists() else Paragraph('',body)])
            data.append(cells)
        widths=[120,65,70,70,105,145,75,90,90,75,230,88]
        t=Table(data,colWidths=widths,rowHeights=None,repeatRows=1)
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#1F4E78')),('GRID',(0,0),(-1,-1),.6,colors.HexColor('#9EADBD')),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('ALIGN',(0,0),(-1,-1),'CENTER'),('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),('LEFTPADDING',(7,1),(8,-1),3),('RIGHTPADDING',(7,1),(8,-1),3),('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#F4F7FA')])]))
        story.append(t)
        if start+2<len(albums): story.append(PageBreak())
    doc.build(story); return publish_temp_file(temp,target)

def find_album_folder(root,album,log=None):
    target=re.search(r'/albums/(\d+)',album.get('url','')); matches=[]
    if target:
        for folder in Path(root).iterdir():
            info_file=folder/'info.json'
            if not folder.is_dir() or not info_file.exists():continue
            try:info=json.loads(info_file.read_text(encoding='utf-8'))
            except Exception:continue
            m=re.search(r'/albums/(\d+)',info.get('url',''))
            if m and m.group(1)==target.group(1):matches.append(folder)
    if not matches:return Path(root)/safe_name(f"{album.get('title','')}_{target.group(1) if target else 'album'}")
    # Prefer the duplicate folder that already contains the richest saved product TXT.
    def detail_score(folder):return sum(bool(v) for v in read_product_detail(folder).values())
    matches.sort(key=lambda d:(detail_score(d),len(list(d.iterdir()))),reverse=True)
    primary=matches[0]
    merged={}
    for folder in matches:
        for key,value in read_product_detail(folder).items():
            if value and not merged.get(key):merged[key]=value
    if merged:write_product_detail(primary,album.get('title',''),album.get('url',''),merged)
    safe_generated=re.compile(r'^\d{3,}\.(?:jpe?g|png|webp|gif)$',re.I)
    for duplicate in matches[1:]:
        contents=list(duplicate.iterdir())
        if all(p.is_file() and (p.name in ('info.json','QR.png','商品详情.txt') or p.name.startswith('ZT') or safe_generated.match(p.name)) for p in contents):
            for item in contents:item.unlink()
            duplicate.rmdir()
            if log:log(f'已合并并清理同一相册的重复目录：{duplicate.name}')
        elif log:log(f'发现同一相册的重复目录，因包含非程序文件暂未删除：{duplicate.name}')
    return primary

def load_saved_albums(root, log):
    root=Path(root); folders=sorted([d for d in root.iterdir() if d.is_dir() and (d/'info.json').exists()])
    if not folders: raise RuntimeError('未找到已下载的相册信息。请先点击“仅下载图片”。')
    manifest_path=root/'download_manifest.json'
    if manifest_path.exists():
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        if not manifest.get('complete',False): raise RuntimeError('上次下载没有正常完成。请先重新点击“仅下载图片”，补齐后再生成。')
        if int(manifest.get('album_count',-1)) != len(folders): raise RuntimeError(f'下载清单应有 {manifest.get("album_count")} 个相册文件夹，实际找到 {len(folders)} 个。请重新下载完成后再生成。')
    albums=[]; problems=[]
    for folder in folders:
        info_file=folder/'info.json'
        try: a=json.loads(info_file.read_text(encoding='utf-8'))
        except Exception as e: problems.append(f'{folder.name}: info.json 读取失败（{e}）'); continue
        expected=a.get('expected_image_count')
        if expected is None:
            try: expected=len(get_album_images(a)) if a.get('url') else None
            except Exception as e: problems.append(f'{folder.name}: 无法核对预期图片数（{e}）'); continue
        images=a.get('images') or []; missing=[]
        for name in images:
            path=Path(name)
            if not path.exists():
                alternative=folder/path.name
                if alternative.exists(): continue
                missing.append(path.name)
        downloaded=int(a.get('image_count',len(images)))
        if expected is None or int(expected)<1 or downloaded!=int(expected) or len(images)!=int(expected) or missing:
            detail=f'预期 {expected} 张，记录下载 {downloaded} 张，记录文件 {len(images)} 个'
            if missing: detail+=f'，缺失文件：{", ".join(missing[:4])}'
            problems.append(f'{folder.name}: 下载未完成（{detail}）'); continue
        cover=Path(a.get('cover_path','')) if a.get('cover_path') else None
        if not cover or not cover.exists():
            choices=list(folder.glob('ZT*'))
            cover=choices[0] if choices else None
        if not cover or not cover.exists(): problems.append(f'{folder.name}: 缺少 ZT 封面图'); continue
        if not a.get('url'): problems.append(f'{folder.name}: info.json 缺少相册 url'); continue
        qr=folder/'QR.png'
        try: qrcode.make(a['url']).save(qr)
        except Exception as e: problems.append(f'{folder.name}: 详情二维码生成失败（{e}）'); continue
        a['folder']=str(folder);a['cover_path']=str(cover);a['qr_path']=str(qr);a['expected_image_count']=int(expected);a['details']=read_product_detail(folder);albums.append(a)
    if problems:
        raise RuntimeError('下载完整性检查未通过，暂不生成文件：\n'+'\n'.join(problems[:12])+'\n请先使用“仅下载图片”补齐下载。')
    if manifest_path.exists() and len(albums)!=int(manifest.get('album_count',-1)):
        raise RuntimeError(f'清单有 {manifest.get("album_count")} 个相册，完整校验通过 {len(albums)} 个。请重新下载。')
    log(f'完整性检查通过：{len(albums)} 个相册，所有原图和 ZT 封面齐全。')
    return albums

def load_albums_from_excel(book_path,log,image_dir):
    book_path=Path(book_path);root=book_path.parent
    saved=load_saved_albums(root,log);by_id={};by_title={}
    for album in saved:
        m=re.search(r'/albums/(\d+)',album.get('url',''))
        if m:by_id[m.group(1)]=album
        by_title[album.get('title','').strip()]=album
    wb_images=load_workbook(book_path,data_only=False);image_ws=wb_images.active
    image_headers={normalize_header(image_ws.cell(1,c).value):c for c in range(1,image_ws.max_column+1)}
    picture_col=image_headers.get(normalize_header('图片/picture'))
    qr_col=next((c for h,c in image_headers.items() if h.startswith(normalize_header('二维码看详情'))),None)
    image_paths={}
    for image in image_ws._images:
        anchor=image.anchor._from;row=anchor.row+1;col=anchor.col+1
        if col not in (picture_col,qr_col):continue
        ext=(getattr(image,'format',None) or 'png').lower();path=Path(image_dir)/f'excel_row_{row}_col_{col}.{ext}'
        path.write_bytes(image._data());image_paths[(row,col)]=str(path)
    wb=load_workbook(book_path,data_only=True);ws=wb.active;columns,url_col=workbook_field_columns(ws)
    if not url_col:url_col=9 if ws.max_column<=9 else SOURCE_URL_COL
    result=[];unmatched=[]
    for row in range(2,ws.max_row+1):
        title=str(ws.cell(row,1).value or '').strip()
        url=str(ws.cell(row,url_col).value or '').strip() if ws.max_column>=url_col else ''
        if not title and not url:continue
        m=re.search(r'/albums/(\d+)',url);base=by_id.get(m.group(1)) if m else None
        if not base:base=by_title.get(title)
        if not base:unmatched.append(title or f'第{row}行');continue
        album=dict(base);album['title']=title or album.get('title','');album['url']=url or album.get('url','')
        details=dict(base.get('details') or {})
        for field,col in columns.items():details[field]=ws.cell(row,col).value if ws.cell(row,col).value is not None else ''
        album['details']=details
        album['cover_path']=image_paths.get((row,picture_col),'') if picture_col else ''
        if qr_col and (row,qr_col) in image_paths:album['qr_path']=image_paths[(row,qr_col)]
        result.append(album)
    if unmatched:log(f'警告：Excel 中 {len(unmatched)} 行未找到相册，未放入 PDF：{", ".join(unmatched[:6])}')
    if not result:raise RuntimeError('所选 Excel 中没有能匹配到已下载相册的商品行。请选该账号目录中的报价表。')
    missing_images=[a.get('title','') for a in result if not a.get('cover_path')]
    if missing_images:log(f'警告：Excel 中 {len(missing_images)} 行没有嵌入商品图片，PDF 对应图片栏将留空：{", ".join(missing_images[:6])}')
    log(f'已从 Excel 提取 {sum(bool(a.get("cover_path")) for a in result)} 张商品图片用于 PDF。')
    log(f'已按 Excel 行顺序载入 {len(result)} 个商品用于生成 PDF。')
    return result

class App:
    def __init__(self, root, embedded=False):
        self.root=root
        if not embedded:
            root.title(APP_TITLE);root.geometry('880x720');root.minsize(760,620)
        self.q=queue.Queue();self.busy=False
        style=ttk.Style();style.configure('TButton',padding=7);style.configure('Header.TLabel',font=('Microsoft YaHei UI',15,'bold'))
        main=ttk.Frame(root,padding=16);main.pack(fill='both',expand=True)
        ttk.Label(main,text=APP_TITLE,style='Header.TLabel').pack(anchor='w',pady=(0,12))
        ttk.Label(main,text='Yupoo 类目链接（支持批量，每行一个 URL）').pack(anchor='w')
        row=ttk.Frame(main);row.pack(fill='x',pady=(4,10));self.url=tk.Text(row,height=3,wrap='word');self.url.pack(side='left',fill='x',expand=True);self.url.insert('1.0','https://he588123.x.yupoo.com/categories');ttk.Button(row,text='打开首个链接',command=self.open_link).pack(side='left',padx=(8,0))
        ttk.Label(main,text='保存位置').pack(anchor='w');row2=ttk.Frame(main);row2.pack(fill='x',pady=(4,10));self.dest=ttk.Entry(row2);self.dest.pack(side='left',fill='x',expand=True);self.dest.insert(0,str(Path.home()/'Desktop'/'Yupoo下载'));ttk.Button(row2,text='浏览…',command=self.browse).pack(side='left',padx=(8,0))
        ttk.Label(main,text='下载与更新').pack(anchor='w',pady=(4,2))
        opts=ttk.Frame(main);opts.pack(fill='x',pady=(0,6));self.make_xlsx=tk.BooleanVar(value=True);self.make_pdf=tk.BooleanVar(value=False);ttk.Label(opts,text='下载完成后生成 Excel；PDF 请在下一步选择 Excel 生成。').pack(side='left')
        self.action_buttons=[]
        download_row=ttk.Frame(main);download_row.pack(fill='x',pady=(0,8))
        self.start=ttk.Button(download_row,text='下载图片 + 生成 Excel',command=lambda:self.start_job('all'));self.start.pack(side='left',padx=(0,8))
        b=ttk.Button(download_row,text='仅下载/更新图片',command=lambda:self.start_job('download'));b.pack(side='left');self.action_buttons.append(b)
        self.force_download=tk.BooleanVar(value=False);ttk.Checkbutton(download_row,text='强制下载（忽略记录，重抓本批链接）',variable=self.force_download).pack(side='left',padx=(12,0))
        ttk.Label(main,text='第二步：根据 Excel 生成 PDF').pack(anchor='w',pady=(2,2))
        gen_row=ttk.Frame(main);gen_row.pack(fill='x',pady=(0,8))
        self.pdf_from_excel_button=ttk.Button(gen_row,text='选择 Excel 并生成 PDF',command=self.choose_excel_for_pdf);self.pdf_from_excel_button.pack(side='left',padx=(0,8));self.action_buttons.append(self.pdf_from_excel_button)
        b=ttk.Button(gen_row,text='单独生成 Excel',command=lambda:self.start_job('excel'));b.pack(side='left');self.action_buttons.append(b)
        ttk.Label(main,text='商品信息回写').pack(anchor='w',pady=(2,2))
        sync_row=ttk.Frame(main);sync_row.pack(fill='x',pady=(0,8))
        self.sync_button=ttk.Button(sync_row,text='选择 Excel 并回写商品详情 TXT',command=self.choose_excel_to_sync);self.sync_button.pack(side='left');self.action_buttons.append(self.sync_button)
        self.progress=ttk.Progressbar(main,mode='indeterminate');self.progress.pack(fill='x',pady=4)
        ttk.Label(main,text='运行记录').pack(anchor='w',pady=(8,2));self.logbox=tk.Text(main,height=14,wrap='word',state='disabled',font=('Consolas',9));self.logbox.pack(fill='both',expand=True)
        self.root.after(100,self.poll)
    def log(self,text): self.q.put(('log',text))
    def open_link(self):
        lines=[x.strip() for x in self.url.get('1.0','end').splitlines() if x.strip()]
        if lines:webbrowser.open(lines[0].replace(chr(92),'/'))
    def browse(self):
        p=filedialog.askdirectory()
        if p: self.dest.delete(0,'end');self.dest.insert(0,p)
    def choose_excel_to_sync(self):
        initial=Path(self.dest.get().strip())
        path=filedialog.askopenfilename(title='选择已填写商品字段的 Excel',initialdir=str(initial if initial.exists() else Path.home()),filetypes=[('Excel 工作簿','*.xlsx *.xlsm'),('所有文件','*.*')])
        if not path:return
        self.busy=True;self.start.configure(state='disabled')
        for b in self.action_buttons:b.configure(state='disabled')
        self.progress.start(12)
        threading.Thread(target=self.run_sync_job,args=(Path(path),),daemon=True).start()
    def choose_excel_for_pdf(self):
        initial=Path(self.dest.get().strip())
        path=filedialog.askopenfilename(title='选择要用于生成 PDF 的 Excel',initialdir=str(initial if initial.exists() else Path.home()),filetypes=[('Excel 工作簿','*.xlsx *.xlsm'),('所有文件','*.*')])
        if path:self.start_pdf_regeneration(Path(path),do_sync=True)
    def run_sync_job(self,book_path):
        try:
            root=book_path.parent
            count,saved_book=sync_excel_to_txt(root,self.log,book_path)
            self.q.put(('sync_done',(str(root),count,str(saved_book))))
        except Exception as e:self.q.put(('error',str(e)+'\n'+traceback.format_exc()))
    def start_pdf_regeneration(self,book_path,do_sync=True):
        self.busy=True;self.start.configure(state='disabled')
        for b in self.action_buttons:b.configure(state='disabled')
        self.progress.start(12)
        threading.Thread(target=self.run_pdf_job,args=(Path(book_path),do_sync),daemon=True).start()
    def run_pdf_job(self,book_path,do_sync=True):
        try:
            if do_sync:_,book_path=sync_excel_to_txt(book_path.parent,self.log,book_path)
            with tempfile.TemporaryDirectory(prefix='YupooPdfExcel_') as image_dir:
                albums=load_albums_from_excel(book_path,self.log,image_dir)
                saved=write_pdf(albums,book_path.with_suffix('.pdf'))
            self.log(f'PDF 已根据 Excel 生成：{saved}')
            self.q.put(('done',f'PDF 已根据所选 Excel 的内容和行顺序生成：\n{saved}'))
        except Exception as e:self.q.put(('error',str(e)+'\n'+traceback.format_exc()))
    def start_job(self,mode='all'):
        if self.busy:return
        if not self.url.get('1.0','end').strip(): messagebox.showerror('缺少链接','请输入至少一个 Yupoo 类目链接，每行一个。');return
        if mode=='all' and not self.make_xlsx.get() and not self.make_pdf.get():messagebox.showerror('未选择输出','至少选择生成 Excel 或 PDF。');return
        self.run_url=self.url.get('1.0','end').strip(); self.run_dest=self.dest.get().strip(); self.run_flags=(bool(self.make_xlsx.get()),bool(self.make_pdf.get()));self.run_force=bool(self.force_download.get())
        self.task_id=create_task('download','Yupoo',self.run_url,self.run_dest)
        self.busy=True;self.start.configure(state='disabled')
        for b in self.action_buttons:b.configure(state='disabled')
        self.progress.start(12)
        threading.Thread(target=self.run_job,args=(mode,),daemon=True).start()
    def run_job(self,mode='all'):
        try:
            raw_lines=[x.strip().replace(chr(92),'/') for x in self.run_url.splitlines() if x.strip()]
            urls=[]
            for url in raw_lines:
                url=re.sub(r'^(https?):/+',lambda m:m.group(1)+'://',url,flags=re.I).rstrip('/')
                if url not in urls:urls.append(url)
            groups={}
            for url in urls:
                host=urlparse(url).hostname or '';account=safe_name(host.split('.')[0] if host else 'Yupoo')
                if account.lower() in ('www','x','yupoo'):account='Yupoo'
                groups.setdefault(account,[]).append(url)
            if not groups:raise RuntimeError('没有可处理的 URL。')
            summaries=[]
            for index,(account,links) in enumerate(groups.items(),1):
                self.log(f'批次 {index}/{len(groups)}：账号 {account}，链接 {len(links)} 个。')
                try:summaries.append(self.run_account_job(mode,account,links))
                except Exception as e:
                    self.log(f'账号 {account} 处理失败：{type(e).__name__}: {e}')
                    self.log(traceback.format_exc());summaries.append(f'{account}: 失败 - {type(e).__name__}: {e}')
            status='完成' if summaries and all('失败' not in x for x in summaries) else '部分失败'
            update_task(self.task_id,status=status,product_count=len(urls),output_path=self.run_dest,detail='\n\n'.join(summaries))
            self.q.put(('done','\n\n'.join(summaries)))
        except Exception as e:
            update_task(self.task_id,status='失败',output_path=self.run_dest,detail=str(e)+'\n'+traceback.format_exc())
            self.q.put(('error',str(e)+'\n'+traceback.format_exc()))
    def run_account_job(self,mode,account,links):
        selected_root=Path(self.run_dest)
        root=selected_root if selected_root.name.casefold()==account.casefold() else selected_root/account
        root.mkdir(parents=True,exist_ok=True);self.log(f'账号文件夹：{root}')
        selected_root.mkdir(parents=True,exist_ok=True);download_log=selected_root/DOWNLOAD_LOG_NAME;records=load_download_log(download_log)
        name=f'{account}_商品报价表'
        xlsx_path=root/(name+'.xlsx')
        existing_xlsx=xlsx_path if xlsx_path.exists() else root/'Yupoo_商品报价表.xlsx'
        if mode in ('all','download'):
            if existing_xlsx.exists():sync_excel_to_txt(root,self.log,existing_xlsx)
            album_map={};needs_download=[];checked_at=time.strftime('%Y-%m-%d %H:%M:%S')
            for url in links:
                found=discover_albums(url,self.log);album_ids={str(a['id']) for a in found};old=records.get((account,url),{});old_count=old.get('产品数量')
                fingerprint=hashlib.sha256(','.join(sorted(album_ids)).encode('utf-8')).hexdigest()[:16]
                try:count_same=int(old_count)==len(album_ids) and (not old.get('相册ID校验') or str(old.get('相册ID校验'))==fingerprint)
                except (TypeError,ValueError):count_same=False
                files_ok=count_same and all(album_files_complete(root,album_id) for album_id in album_ids)
                if not self.run_force and files_ok:
                    self.log(f'链接无需更新：产品数 {len(album_ids)} 未变化且本地文件完整。 {url}')
                    records[(account,url)]={'供货商/account':account,'链接/url':url,'产品数量':len(album_ids),'最近检查时间':checked_at,'下载状态':'跳过（内容未变且文件完整）','本地目录':str(root),'相册ID校验':fingerprint}
                    continue
                reason='强制下载' if self.run_force else ('产品数量变化' if not count_same else '本地文件缺失或不完整')
                self.log(f'链接需要更新：{reason}，当前产品数 {len(album_ids)}。 {url}')
                needs_download.append(url)
                for album in found:album_map.setdefault(album['id'],album)
                records[(account,url)]={'供货商/account':account,'链接/url':url,'产品数量':len(album_ids),'最近检查时间':checked_at,'下载状态':'更新中','本地目录':str(root),'相册ID校验':fingerprint}
            save_download_log(download_log,records)
            if not album_map:
                self.log(f'总记录已更新：{download_log}')
                if mode=='download':return f'{account}: 所有链接数量未变且本地文件完整，已跳过下载。\n{root}'
                albums=load_saved_albums(root,self.log)
                if not albums:raise RuntimeError(f'{account}: 记录显示无需更新，但本地没有可用商品文件。请勾选“强制下载”。')
                # Continue below to regenerate requested output files from the local copies.
                skip_download=True
            else:skip_download=False
            if skip_download:
                ids={str(a['id']) for a in albums}
            else:
                ids={str(a['id']) for a in album_map.values()}
            albums=list(album_map.values()) if not skip_download else albums
            if not albums:raise RuntimeError(f'{account}: 链接中没有找到相册。')
            manifest_path=root/'download_manifest.json';prior_ids=set()
            if manifest_path.exists():
                try:
                    old=json.loads(manifest_path.read_text(encoding='utf-8'));prior_ids.update(map(str,old.get('album_ids',[])))
                except Exception:pass
            for folder in root.iterdir():
                if folder.is_dir() and (folder/'info.json').exists():
                    try:
                        oldinfo=json.loads((folder/'info.json').read_text(encoding='utf-8'));m=re.search(r'/albums/(\d+)',oldinfo.get('url',''))
                        if m:prior_ids.add(m.group(1))
                    except Exception:pass
            ids=prior_ids|{str(a['id']) for a in albums}
            prior_urls=[]
            if manifest_path.exists():
                try:prior_urls=json.loads(manifest_path.read_text(encoding='utf-8')).get('source_urls',[])
                except Exception:pass
            source_urls=list(dict.fromkeys([*prior_urls,*links]))
            manifest={'account':account,'source_urls':source_urls,'album_ids':sorted(ids),'album_count':len(ids),'complete':False}
            manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
            for i,a in enumerate(albums,1) if not skip_download else ():
                folder=find_album_folder(root,a,self.log);folder.mkdir(parents=True,exist_ok=True);a['folder']=str(folder)
                for oldfile in folder.iterdir():
                    if oldfile.is_file() and oldfile.name!='商品详情.txt' and oldfile.suffix.lower() in ('.jpg','.jpeg','.png','.webp','.gif'):oldfile.unlink()
                a['details']=write_product_detail(folder,a['title'],a['url'])
                self.log(f'[{i}/{len(albums)}] {a["title"]}')
                imgs=get_album_images(a)
                if not a.get('cover_url') and imgs:a['cover_url']=imgs[0]
                cover_url=a.get('cover_url','');cover_parent=urlparse(cover_url).path.rsplit('/',1)[0] if cover_url else ''
                if len(imgs)>1 and cover_parent:imgs=[u for u in imgs if urlparse(u).path.rsplit('/',1)[0]!=cover_parent]
                cover_path=''
                if cover_url:
                    original=Path(urlparse(cover_url).path).name or 'medium.jpeg';cover_path=folder/('ZT'+original)
                    try:download_one(cover_url,cover_path,a['url'])
                    except Exception as e:self.log(f'封面下载失败，将尝试用相册首图补作封面：{e}');cover_path=''
                saved=[]
                for j,u in enumerate(imgs,1):
                    ext=Path(urlparse(u).path).suffix or '.jpg';target=folder/f'{j:03d}{ext}'
                    try:download_one(u,target,a['url']);saved.append(str(target))
                    except Exception as e:self.log(f'图片失败：{u}（{e}）')
                if not cover_path and saved:
                    source=Path(saved[0]);cover_path=folder/f'ZT{source.name}';cover_path.write_bytes(source.read_bytes())
                a['folder']=str(folder);a['cover_path']=str(cover_path) if cover_path else ''
                a['expected_image_count']=len(imgs);a['image_count']=len(saved);a['images']=saved
                qr_path=folder/'QR.png';qrcode.make(a['url']).save(qr_path);a['qr_path']=str(qr_path)
                (folder/'info.json').write_text(json.dumps(a,ensure_ascii=False,indent=2),encoding='utf-8')
                detail=read_product_detail(folder)
                missing=[label for key,label in (('单价/price(RMB)','采购单价'),('箱规/Carton dimensions','箱规尺寸'),('单箱毛重/Gross weight kg','单箱毛重')) if not str(detail.get(key) or '').strip()]
                record_source('Yupoo',account,a['title'],str(folder),a['url'],'待补资料' if missing else '待复核',missing)
                self.log(f'  {len(saved)}/{len(imgs)} 张图片，封面 {Path(cover_path).name if cover_path else "缺失"}')
            # Collapse historical duplicate folders, preserving the richest product TXT.
            ids_now=set()
            for folder in list(root.iterdir()):
                if folder.is_dir() and (folder/'info.json').exists():
                    try:
                        oldinfo=json.loads((folder/'info.json').read_text(encoding='utf-8'));m=re.search(r'/albums/(\d+)',oldinfo.get('url',''))
                        if m and m.group(1) in ids:find_album_folder(root,oldinfo,self.log);ids_now.add(m.group(1))
                    except Exception:pass
            status={}
            for folder in [d for d in root.iterdir() if d.is_dir() and (d/'info.json').exists()]:
                try:
                    info=json.loads((folder/'info.json').read_text(encoding='utf-8'));m=re.search(r'/albums/(\d+)',info.get('url',''))
                    if not m:continue
                    paths=info.get('images') or [];expected=int(info.get('expected_image_count',-1))
                    status[m.group(1)]=expected>0 and expected==int(info.get('image_count',-1))==len(paths) and all(Path(x).exists() for x in paths) and bool(info.get('cover_path')) and Path(info['cover_path']).exists()
                except Exception:continue
            manifest['complete']=all(status.get(album_id,False) for album_id in ids);manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
            for url in needs_download:
                rec=records.get((account,url),{});rec['下载状态']='完成' if manifest['complete'] else '下载未完整';rec['最近检查时间']=time.strftime('%Y-%m-%d %H:%M:%S');records[(account,url)]=rec
            save_download_log(download_log,records);self.log(f'总记录已更新：{download_log}')
            if mode=='download':return f'{account}: '+('图片下载完成并通过检查。' if manifest['complete'] else '下载有缺失，请查看运行记录。')+f'\n{root}'
            albums=load_saved_albums(root,self.log)
        else:
            if existing_xlsx.exists():sync_excel_to_txt(root,self.log,existing_xlsx)
            albums=load_saved_albums(root,self.log)
        make_xlsx=mode in ('excel','both') or (mode=='all' and self.run_flags[0])
        make_pdf=mode in ('pdf','both') or (mode=='all' and self.run_flags[1])
        if make_xlsx or make_pdf:apply_exchange_rate_to_albums(albums,self.log)
        if make_xlsx:
            saved=write_excel(albums,root,xlsx_path);self.log(f'Excel 已生成：{saved}')
        if make_pdf:
            saved=write_pdf(albums,root/(name+'.pdf'));self.log(f'PDF 已生成：{saved}')
        return f'{account}: 处理完成。\n{root}'
    def poll(self):
        try:
            while True:
                kind,msg=self.q.get_nowait()
                if kind=='log':self.logbox.configure(state='normal');self.logbox.insert('end',msg+'\n');self.logbox.see('end');self.logbox.configure(state='disabled')
                elif kind=='done':self.progress.stop();self.start.configure(state='normal');[b.configure(state='normal') for b in self.action_buttons];self.busy=False;messagebox.showinfo('处理完成',msg)
                elif kind=='sync_done':
                    self.progress.stop();self.start.configure(state='normal');[b.configure(state='normal') for b in self.action_buttons];self.busy=False
                    root,count,book_path=msg; self.log(f'回写完成：{count} 行。')
                    if messagebox.askyesno('回写完成','Excel 字段已回写到各商品详情 TXT。\n是否立即根据所选 Excel 生成 PDF？'):
                        self.start_pdf_regeneration(Path(book_path),do_sync=False)
                    else:messagebox.showinfo('回写完成',f'商品详情 TXT 已更新。\n{root}')
                elif kind=='error':self.progress.stop();self.start.configure(state='normal');[b.configure(state='normal') for b in self.action_buttons];self.busy=False;self.logbox.configure(state='normal');self.logbox.insert('end',msg+'\n');self.logbox.see('end');self.logbox.configure(state='disabled');messagebox.showerror('处理失败',msg.splitlines()[0])
        except queue.Empty:pass
        self.root.after(120,self.poll)

def main():
    root=tk.Tk();App(root);root.mainloop()
if __name__=='__main__':
    if len(sys.argv)>2 and sys.argv[1]=='--check-ssl':
        data,ctype=http_get(sys.argv[2]); print('HTTPS_OK',len(data),ctype); raise SystemExit(0)
    main()











