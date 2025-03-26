import re
import sqlite3
import requests
from bs4 import BeautifulSoup
import time
from urllib.parse import urljoin, urlparse
import random
import json
from urllib.robotparser import RobotFileParser
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from datetime import datetime
import os
import webbrowser
from tabulate import tabulate
from collections import Counter, OrderedDict
from queue import PriorityQueue
import psutil
from nltk.stem import SnowballStemmer
import matplotlib.pyplot as plt


DEFAULT_CONFIG = {
    "user_agents": [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Safari/605.1.15"
    ],
    "excluded_extensions": ['.pdf', '.jpg', '.jpeg', '.png', '.gif', '.zip', '.exe'],
    "max_retries": 3,
    "request_timeout": 15,
    "delay_range": [1, 5],
    "max_workers": 5,
    "db_name": "spider_results.db",
    "json_export": "spider_results.json",
    "max_title_length": 50,
    "max_desc_length": 100,
    "max_pages": 50,
    "max_depth": 4,
    "top_keywords_limit": 20,
    "top_pages_limit": 10,
    "max_redirects": 5,
    "memory_limit": 90,
    "content_size_limit": 1024 * 1024 * 5,
    "allowed_domains": [],
    "respect_robots": True,
    "use_proxies": False,
    "proxy_list": [],
    "stop_words": ['el', 'la', 'los', 'de', 'en', 'y', 'a'],
    "keywords_boost": {
        'contacto': 0.3,
        'about': 0.2,
        'servicios': 0.2
    }
}

def load_config():
    """Carga configuración con validación avanzada"""
    try:
        with open('spider_config.json', encoding='utf-8') as f:
            config = json.load(f)
            
            if not isinstance(config.get('user_agents', []), list):
                raise ValueError("user_agents debe ser una lista")
                
            if not 1 <= config.get('max_depth', 3) <= 10:
                raise ValueError("max_depth debe estar entre 1 y 10")
                
            default_values = {
                'max_pages': min(config.get('max_pages', 50), 500),
                'max_depth': min(config.get('max_depth', 4), 8),
                'request_timeout': min(config.get('request_timeout', 15), 60),
                'delay_range': sorted(config.get('delay_range', [1, 5]))[:2]
            }
            
            return {**DEFAULT_CONFIG, **config, **default_values}
            
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        print(f"⚠️ Error en configuración: {str(e)} - Usando valores por defecto")
        return DEFAULT_CONFIG

CONFIG = load_config()


def init_database():
    """Inicializa la base de datos con estructura mejorada"""
    conn = sqlite3.connect(CONFIG['db_name'])
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS pages (
                url TEXT PRIMARY KEY,
                depth INTEGER,
                title TEXT,
                http_status INTEGER,
                load_time REAL,
                timestamp DATETIME,
                word_count INTEGER,
                link_count INTEGER,
                content_size INTEGER)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS seo_data (
                url TEXT PRIMARY KEY,
                meta_description TEXT,
                h1_count INTEGER,
                h2_count INTEGER,
                image_count INTEGER,
                internal_links INTEGER,
                external_links INTEGER,
                has_canonical INTEGER)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS backlinks (
                from_url TEXT,
                to_url TEXT,
                anchor_text TEXT,
                PRIMARY KEY (from_url, to_url))''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS content_analysis (
                url TEXT,
                word TEXT,
                frequency INTEGER,
                PRIMARY KEY (url, word))''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME,
                pages_crawled INTEGER,
                avg_load_time REAL,
                memory_usage REAL)''')
    
    conn.commit()
    conn.close()


def get_random_user_agent():
    return random.choice(CONFIG['user_agents'])

def can_fetch(url):
    """Verifica robots.txt"""
    if not CONFIG['respect_robots']:
        return True
        
    robot_parser = RobotFileParser()
    robot_url = urljoin(url, '/robots.txt')
    robot_parser.set_url(robot_url)
    
    try:
        robot_parser.read()
        return robot_parser.can_fetch(get_random_user_agent(), url)
    except Exception as e:
        print(f"⚠️ Error al leer robots.txt: {str(e)}")
        return True

def fetch_page(session, url):
    """Obtiene página con manejo de errores"""
    headers = {'User-Agent': get_random_user_agent()}
    
    for attempt in range(CONFIG['max_retries']):
        try:
            start_time = time.time()
            response = session.get(
                url, 
                headers=headers, 
                timeout=CONFIG['request_timeout'],
                allow_redirects=True,
                verify=False
            )
            
            content_type = response.headers.get('Content-Type', '')
            if 'text/html' not in content_type:
                return None, 415, 0
                
            if len(response.content) > CONFIG['content_size_limit']:
                return None, 413, 0
                
            response.raise_for_status()
            return response.text, response.status_code, time.time() - start_time
            
        except requests.exceptions.SSLError:
            try:
                response = session.get(url, headers=headers, 
                                    timeout=CONFIG['request_timeout'],
                                    verify=False)
                return response.text, response.status_code, time.time() - start_time
            except Exception:
                return None, 495, 0
            
        except requests.exceptions.TooManyRedirects:
            return None, 310, 0
            
        except requests.exceptions.RequestException as e:
            if attempt == CONFIG['max_retries'] - 1:
                status_code = getattr(e.response, 'status_code', 500)
                if isinstance(e, requests.exceptions.Timeout):
                    status_code = 408
                elif isinstance(e, requests.exceptions.ConnectionError):
                    status_code = 503
                return None, status_code, 0
            time.sleep(2 ** attempt)
            
    return None, 500, 0

def extract_links(html, base_url):
    """Extrae enlaces con texto de anclaje"""
    soup = BeautifulSoup(html, 'html.parser')
    links = set()
    domain = urlparse(base_url).netloc
    
    for a_tag in soup.find_all('a', href=True):
        link = urljoin(base_url, a_tag['href'])
        parsed_link = urlparse(link)
        
        if any(parsed_link.path.endswith(ext) for ext in CONFIG['excluded_extensions']):
            continue
            
        if CONFIG['allowed_domains'] and parsed_link.netloc not in CONFIG['allowed_domains']:
            continue
            
        anchor_text = a_tag.get_text(strip=True)[:100]
        links.add((link, anchor_text))
        
    return links

def extract_seo_data(html, base_url):
    """Extrae datos SEO"""
    soup = BeautifulSoup(html, 'html.parser')
    domain = urlparse(base_url).netloc
    
    meta_description = ""
    canonical = False
    for meta in soup.find_all('meta'):
        if meta.get('name', '').lower() == 'description':
            meta_description = meta.get('content', '')[:CONFIG['max_desc_length']]
    
    for link in soup.find_all('link', rel=True):
        if link['rel'] == 'canonical':
            canonical = True
    
    h1_count = len(soup.find_all('h1'))
    h2_count = len(soup.find_all('h2'))
    image_count = len(soup.find_all('img'))
    
    internal_links = 0
    external_links = 0
    for a in soup.find_all('a', href=True):
        if urlparse(a['href']).netloc == domain:
            internal_links += 1
        else:
            external_links += 1
    
    return {
        'meta_description': meta_description,
        'h1_count': h1_count,
        'h2_count': h2_count,
        'image_count': image_count,
        'internal_links': internal_links,
        'external_links': external_links,
        'has_canonical': int(canonical)
    }

def analyze_content(text, url):
    """Analiza contenido"""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text)
    
    words = [word.lower() for word in re.findall(r'\b\w{3,}\b', text) 
             if word.lower() not in CONFIG['stop_words']]
    
    try:
        stemmer = SnowballStemmer('spanish')
        words = [stemmer.stem(word) for word in words]
    except:
        pass
    
    word_freq = Counter(words)
    
    conn = sqlite3.connect(CONFIG['db_name'])
    try:
        conn.execute('BEGIN TRANSACTION')
        conn.execute('DELETE FROM content_analysis WHERE url = ?', (url,))
        
        for word, freq in word_freq.most_common(100):
            conn.execute('INSERT INTO content_analysis VALUES (?, ?, ?)', 
                        (url, word, freq))
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        print(f"⚠️ Error en DB al analizar contenido: {str(e)}")
    finally:
        conn.close()
    
    return word_freq.most_common(10)

def get_page_priority(url, depth):
    """Calcula prioridad de página"""
    priority = depth * 0.5
    
    for keyword, boost in CONFIG['keywords_boost'].items():
        if keyword in url.lower():
            priority -= boost
            
    return priority


def spider(start_url, max_depth=CONFIG['max_depth'], max_pages=CONFIG['max_pages']):
    """Spider principal"""
    init_database()
    session = requests.Session()
    session.max_redirects = CONFIG['max_redirects']
    parsed_start_url = urlparse(start_url)
    domain = parsed_start_url.netloc
    
    visited = set()
    to_visit = [(get_page_priority(start_url, 0), start_url, 0)]
    lock = threading.Lock()
    crawl_queue = PriorityQueue()
    
    stats = {
        'success': 0,
        'failed': 0,
        'redirects': 0,
        'total_size': 0,
        'start_time': time.time(),
        'memory_usage': []
    }
    
    def process_page(url, depth):
        nonlocal visited, stats
        
        mem_usage = psutil.virtual_memory().percent
        stats['memory_usage'].append(mem_usage)
        
        if mem_usage > CONFIG['memory_limit']:
            print(f"⚠️ Advertencia: Uso alto de memoria ({mem_usage}%)")
            return None
            
        if (url in visited or depth > max_depth or len(visited) >= max_pages):
            return None
            
        print(f"🌐 [{len(visited)+1}/{max_pages}] Visitando: {url}")
        
        if not can_fetch(url):
            print(f"🚫 Bloqueado por robots.txt: {url}")
            return None
            
        html, status_code, load_time = fetch_page(session, url)
        if not html:
            return None
            
        soup = BeautifulSoup(html, 'html.parser')
        title = (soup.title.string if soup.title else "")[:CONFIG['max_title_length']]
        text = soup.get_text(separator=' ', strip=True)
        links_with_anchors = extract_links(html, url)
        links = [link for link, _ in links_with_anchors]
        seo_data = extract_seo_data(html, url)
        word_count = len(text.split())
        content_size = len(html.encode('utf-8'))
        analyze_content(text, url)
        
        with lock:
            if status_code == 200:
                stats['success'] += 1
                stats['total_size'] += content_size
            elif 300 <= status_code < 400:
                stats['redirects'] += 1
            else:
                stats['failed'] += 1
        
        conn = sqlite3.connect(CONFIG['db_name'])
        try:
            conn.execute('''INSERT OR REPLACE INTO pages
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        (url, depth, title, status_code, load_time, 
                         datetime.now(), word_count, len(links), content_size))
            
            conn.execute('''INSERT OR REPLACE INTO seo_data
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                        (url, seo_data['meta_description'], seo_data['h1_count'],
                         seo_data['h2_count'], seo_data['image_count'],
                         seo_data['internal_links'], seo_data['external_links'],
                         seo_data['has_canonical']))
            
            for link, anchor in links_with_anchors:
                conn.execute('''INSERT OR IGNORE INTO backlinks
                               VALUES (?, ?, ?)''', (url, link, anchor))
            
            conn.execute('''INSERT INTO stats
                          (timestamp, pages_crawled, avg_load_time, memory_usage)
                          VALUES (?, ?, ?, ?)''',
                       (datetime.now(), len(visited)+1, load_time, mem_usage))
            
            conn.commit()
            
            with lock:
                visited.add(url)
                new_links = []
                for link in links:
                    if (urlparse(link).netloc == domain and
                        (depth + 1) <= max_depth and
                        link not in visited and
                        len(visited) + len(to_visit) < max_pages):
                        
                        priority = get_page_priority(link, depth + 1)
                        new_links.append((priority, link, depth + 1))
                
                return new_links
                
        except sqlite3.Error as e:
            print(f"⚠️ Error en DB: {str(e)}")
            return None
        finally:
            conn.close()
            time.sleep(random.uniform(*CONFIG['delay_range']))
    
    with ThreadPoolExecutor(max_workers=CONFIG['max_workers']) as executor:
        futures = []
        
        for priority, url, depth in to_visit:
            crawl_queue.put((priority, url, depth))
        
        while not crawl_queue.empty() and len(visited) < max_pages:
            _, url, depth = crawl_queue.get()
            futures.append(executor.submit(process_page, url, depth))
            
            if len(futures) >= CONFIG['max_workers'] * 2:
                for future in as_completed(futures):
                    if new_links := future.result():
                        with lock:
                            for link_data in new_links:
                                crawl_queue.put(link_data)
                futures = []
        
        for future in as_completed(futures):
            if new_links := future.result():
                with lock:
                    for link_data in new_links:
                        crawl_queue.put(link_data)
    
    conn = sqlite3.connect(CONFIG['db_name'])
    try:
        conn.execute('''INSERT INTO stats
                      (timestamp, pages_crawled, avg_load_time, memory_usage)
                      VALUES (?, ?, ?, ?)''',
                   (datetime.now(), len(visited), 
                    stats['total_size'] / max(1, len(visited)),
                    sum(stats['memory_usage'])/max(1, len(stats['memory_usage']))))
        conn.commit()
    finally:
        conn.close()
    
    return list(visited)


def generate_report():
    """Genera reportes"""
    conn = sqlite3.connect(CONFIG['db_name'])
    
    print("\n" + "="*80)
    print("🕷️ WEB SPIDER REPORT - ANALISIS AVANZADO".center(80))
    print("="*80)
    
    print("\n📊 RESUMEN ESTADÍSTICO AVANZADO")
    print("-"*80)
    
    total_pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    status_codes = conn.execute('''SELECT http_status, COUNT(*) 
                                 FROM pages GROUP BY http_status''').fetchall()
    
    status_table = []
    for code, count in status_codes:
        percent = (count / total_pages) * 100
        status_table.append([code, count, f"{percent:.1f}%"])
    
    print(tabulate(status_table, 
                  headers=["Código", "Cantidad", "Porcentaje"],
                  tablefmt="grid"))
    
    print("\n⏱️ TIEMPOS DE CARGA Y USO DE RECURSOS")
    print("-"*80)
    load_stats = conn.execute('''SELECT 
                               MIN(load_time), 
                               MAX(load_time), 
                               AVG(load_time),
                               SUM(load_time),
                               AVG(memory_usage)
                               FROM stats''').fetchone()
    
    print(tabulate([
        ["Mínimo", f"{load_stats[0]:.2f}s"],
        ["Máximo", f"{load_stats[1]:.2f}s"],
        ["Promedio", f"{load_stats[2]:.2f}s"],
        ["Total", f"{load_stats[3]:.2f}s"],
        ["Memoria promedio", f"{load_stats[4]:.1f}%"]
    ], tablefmt="grid"))
    
    print("\n📊 PÁGINAS MÁS RELEVANTES (SEO + CONTENIDO)")
    print("-"*80)
    important_pages = conn.execute('''SELECT p.url, p.title, p.word_count, p.link_count, 
                                    s.h1_count, s.h2_count, s.image_count
                                    FROM pages p
                                    JOIN seo_data s ON p.url = s.url
                                    ORDER BY p.word_count DESC LIMIT 5''').fetchall()
    print(tabulate(important_pages, 
                  headers=["URL", "Título", "Palabras", "Enlaces", "H1", "H2", "Imágenes"],
                  tablefmt="grid",
                  maxcolwidths=[25, 15, 8, 8, 5, 5, 8]))
    
    print("\n🔍 ANALISIS SEO AVANZADO")
    print("-"*80)
    seo_stats = conn.execute('''SELECT url, 
                              (h1_count + h2_count) as headings,
                              internal_links, 
                              external_links,
                              has_canonical
                              FROM seo_data 
                              ORDER BY headings DESC LIMIT 5''').fetchall()
    print(tabulate(seo_stats,
                  headers=["URL", "Encabezados", "Enl. Int.", "Enl. Ext.", "Canonical"],
                  tablefmt="grid",
                  maxcolwidths=[30, 10, 10, 10, 10]))
    
    print("\n🔤 TOP PALABRAS CLAVE (ANALISIS SEMÁNTICO)")
    print("-"*80)
    keywords = conn.execute('''SELECT word, SUM(frequency) as total
                             FROM content_analysis
                             GROUP BY word
                             ORDER BY total DESC
                             LIMIT 15''').fetchall()
    print(tabulate(keywords, headers=["Palabra", "Frecuencia"], tablefmt="grid"))
    
    print("\n🔗 TOP PÁGINAS ENLAZADAS (LINK JUICE)")
    print("-"*80)
    top_linked = conn.execute('''SELECT b.to_url, COUNT(*) as links, p.title
                               FROM backlinks b
                               LEFT JOIN pages p ON b.to_url = p.url
                               GROUP BY b.to_url
                               ORDER BY links DESC
                               LIMIT 5''').fetchall()
    print(tabulate(top_linked, 
                  headers=["URL", "Enlaces", "Título"],
                  tablefmt="grid", 
                  maxcolwidths=[25, 8, 15]))
    
    if input("\n¿Generar gráficos? [s/N]: ").lower() == 's':
        try:
            codes, counts = zip(*status_codes)
            plt.figure(figsize=(10, 5))
            plt.bar([str(c) for c in codes], counts, color='skyblue')
            plt.title("Distribución de Códigos de Estado HTTP")
            plt.xlabel("Código de Estado")
            plt.ylabel("Cantidad de Páginas")
            plt.grid(axis='y', linestyle='--', alpha=0.7)
            plt.show()
            
            keywords = conn.execute('''SELECT word, SUM(frequency) as total
                                     FROM content_analysis
                                     GROUP BY word
                                     ORDER BY total DESC
                                     LIMIT 10''').fetchall()
            words, freqs = zip(*keywords)
            plt.figure(figsize=(12, 6))
            plt.barh(words, freqs, color='salmon')
            plt.title("Top 10 Palabras Clave")
            plt.xlabel("Frecuencia")
            plt.grid(axis='x', linestyle='--', alpha=0.7)
            plt.tight_layout()
            plt.show()
            
            mem_data = conn.execute('''SELECT memory_usage FROM stats
                                     ORDER BY timestamp''').fetchall()
            mem_usage = [m[0] for m in mem_data]
            plt.figure(figsize=(12, 4))
            plt.plot(mem_usage, marker='o', linestyle='-', color='green')
            plt.axhline(y=CONFIG['memory_limit'], color='r', linestyle='--')
            plt.title("Uso de Memoria Durante el Crawling")
            plt.xlabel("Página")
            plt.ylabel("Uso de Memoria (%)")
            plt.grid(True)
            plt.tight_layout()
            plt.show()
            
        except ImportError:
            print("⚠️ matplotlib no instalado - omitiendo gráficos")
    
    conn.close()

def export_to_json():
    """Exporta a JSON"""
    conn = sqlite3.connect(CONFIG['db_name'])
    cursor = conn.cursor()
    
    result_data = OrderedDict([
        ("metadata", OrderedDict([
            ("total_pages", 0),
            ("total_links", 0),
            ("analysis_date", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ("parameters", OrderedDict([
                ("max_depth", CONFIG['max_depth']),
                ("max_pages", CONFIG['max_pages']),
                ("start_url", start_url if 'start_url' in globals() else "")
            ])),
            ("stats", OrderedDict([
                ("success_pages", 0),
                ("failed_pages", 0),
                ("avg_load_time", 0),
                ("avg_memory_usage", 0),
                ("total_content_size", 0)
            ]))
        ])),
        ("pages", []),
        ("top_keywords", []),
        ("top_linked_pages", []),
        ("seo_analysis", OrderedDict([
            ("best_practices", []),
            ("common_issues", [])
        ]))
    ])
    
    cursor.execute("SELECT COUNT(*) FROM pages")
    result_data["metadata"]["total_pages"] = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM backlinks")
    result_data["metadata"]["total_links"] = cursor.fetchone()[0]
    
    cursor.execute('''SELECT 
                     SUM(CASE WHEN http_status = 200 THEN 1 ELSE 0 END),
                     SUM(CASE WHEN http_status != 200 THEN 1 ELSE 0 END),
                     AVG(load_time),
                     SUM(content_size)
                     FROM pages''')
    stats = cursor.fetchone()
    result_data["metadata"]["stats"]["success_pages"] = stats[0]
    result_data["metadata"]["stats"]["failed_pages"] = stats[1]
    result_data["metadata"]["stats"]["avg_load_time"] = round(stats[2] or 0, 2)
    result_data["metadata"]["stats"]["total_content_size"] = stats[3]
    
    cursor.execute('''SELECT p.url, p.depth, p.title, p.http_status, 
                     p.load_time, p.word_count, p.link_count, p.content_size,
                     s.meta_description, s.h1_count, s.h2_count,
                     s.image_count, s.internal_links, s.external_links, s.has_canonical
                     FROM pages p
                     JOIN seo_data s ON p.url = s.url
                     ORDER BY p.depth, p.word_count DESC''')
    
    for row in cursor.fetchall():
        page_data = OrderedDict([
            ("url", row[0]),
            ("depth", row[1]),
            ("title", row[2]),
            ("status", row[3]),
            ("load_time_sec", round(row[4], 2)),
            ("word_count", row[5]),
            ("link_count", row[6]),
            ("content_size_kb", round(row[7] / 1024, 2)),
            ("seo", OrderedDict([
                ("meta_description", row[8]),
                ("h1_count", row[9]),
                ("h2_count", row[10]),
                ("image_count", row[11]),
                ("internal_links", row[12]),
                ("external_links", row[13]),
                ("has_canonical", bool(row[14]))
            ]))
        ])
        result_data["pages"].append(page_data)
    
    cursor.execute('''SELECT word, SUM(frequency) as total
                     FROM content_analysis
                     GROUP BY word
                     ORDER BY total DESC
                     LIMIT ?''', (CONFIG['top_keywords_limit'],))
    
    for row in cursor.fetchall():
        result_data["top_keywords"].append(OrderedDict([
            ("keyword", row[0]),
            ("frequency", row[1])
        ]))
    
    cursor.execute('''SELECT to_url, COUNT(*) as links
                     FROM backlinks
                     GROUP BY to_url
                     ORDER BY links DESC
                     LIMIT ?''', (CONFIG['top_pages_limit'],))
    
    for row in cursor.fetchall():
        result_data["top_linked_pages"].append(OrderedDict([
            ("url", row[0]),
            ("incoming_links", row[1])
        ]))
    
    cursor.execute('''SELECT url FROM seo_data 
                     WHERE LENGTH(meta_description) < 10''')
    result_data["seo_analysis"]["common_issues"].append({
        "issue": "Missing or short meta description",
        "count": len(cursor.fetchall())
    })
    
    cursor.execute('''SELECT url FROM seo_data WHERE h1_count > 1''')
    result_data["seo_analysis"]["common_issues"].append({
        "issue": "Multiple H1 headings",
        "count": len(cursor.fetchall())
    })
    
    cursor.execute('''SELECT url FROM seo_data WHERE has_canonical = 0''')
    result_data["seo_analysis"]["common_issues"].append({
        "issue": "Missing canonical tag",
        "count": len(cursor.fetchall())
    })
    
    conn.close()
    
    with open(CONFIG['json_export'], 'w', encoding='utf-8') as f:
        json.dump(result_data, f, indent=4, ensure_ascii=False)
    
    print(f"\n✅ Datos exportados a {CONFIG['json_export']}")
    return CONFIG['json_export']


def main():
    print("""
    ███████╗██████╗ ██╗██████╗ ███████╗██████╗ 
    ██╔════╝██╔══██╗██║██╔══██╗██╔════╝██╔══██╗
    ███████╗██████╔╝██║██████╔╝█████╗  ██████╔╝
    ╚════██║██╔═══╝ ██║██╔══██╗██╔══╝  ██╔══██╗
    ███████║██║     ██║██║  ██║███████╗██║  ██║
    ╚══════╝╚═╝     ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝
    Web Spider Avanzado v4.0 - Jose Luis Asenjo
    """)
    
    global start_url
    start_url = input("Ingrese la URL inicial (ej: https://example.com): ").strip()
    if not start_url.startswith(('http://', 'https://')):
        start_url = 'https://' + start_url
    
    try:
        parsed = urlparse(start_url)
        if not parsed.netloc:
            raise ValueError("URL no válida")
    except:
        print("❌ Error: URL no válida")
        return
    
    suggested_depth = min(CONFIG['max_depth'], 4)
    suggested_pages = min(CONFIG['max_pages'], 50)
    
    print(f"\nConfiguración sugerida (máx {CONFIG['max_depth']} niveles, {CONFIG['max_pages']} páginas)")
    max_depth = int(input(f"Profundidad máxima [{suggested_depth}]: ").strip() or suggested_depth)
    max_pages = int(input(f"Número máximo de páginas [{suggested_pages}]: ").strip() or suggested_pages)
    
    max_depth = min(max_depth, CONFIG['max_depth'])
    max_pages = min(max_pages, CONFIG['max_pages'])
    
    print("\n🚀 Iniciando análisis extendido...\n")
    print(f"• Profundidad de análisis: {max_depth} niveles")
    print(f"• Límite de páginas: {max_pages}")
    print(f"• Workers paralelos: {CONFIG['max_workers']}")
    print(f"• Delay entre requests: {CONFIG['delay_range'][0]}-{CONFIG['delay_range'][1]}s\n")
    
    start_time = time.time()
    visited = spider(start_url, max_depth, max_pages)
    elapsed_time = time.time() - start_time
    
    print(f"\n✅ Análisis completado en {elapsed_time:.2f} segundos")
    print(f"• Páginas visitadas: {len(visited)}")
    print(f"• Datos guardados en: {CONFIG['db_name']}")
    
    generate_report()
    
    if input("\n¿Exportar resultados a JSON? [S/n]: ").lower() != 'n':
        json_file = export_to_json()
        if input("¿Abrir archivo JSON? [s/N]: ").lower() == 's':
            webbrowser.open(f"file://{os.path.abspath(json_file)}")
    
    if input("\n¿Abrir base de datos completa? [s/N]: ").lower() == 's':
        webbrowser.open(f"file://{os.path.abspath(CONFIG['db_name'])}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Spider detenido por el usuario")
    except Exception as e:
        print(f"\n❌ Error crítico: {str(e)}")