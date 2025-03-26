import sqlite3
from tabulate import tabulate

def mostrar_tabla(nombre_tabla):
    conn = sqlite3.connect('spider_results.db')
    cursor = conn.cursor()
    
    # Obtener datos
    cursor.execute(f"SELECT * FROM {nombre_tabla} LIMIT 10")
    datos = cursor.fetchall()
    
    # Obtener nombres de columnas
    cursor.execute(f"PRAGMA table_info({nombre_tabla})")
    columnas = [col[1] for col in cursor.fetchall()]
    
    # Mostrar tabla
    print(f"\n📊 TABLA: {nombre_tabla.upper()}")
    print(tabulate(datos, headers=columnas, tablefmt="grid"))
    
    conn.close()

# Mostrar todas las tablas importantes
mostrar_tabla('pages')
mostrar_tabla('seo_data')
mostrar_tabla('backlinks')
mostrar_tabla('content_analysis')