import tkinter as tk
from tkinter import filedialog, ttk, messagebox
import fitz  # PyMuPDF
import edge_tts
import asyncio
import pygame
import threading
import os
import re
import tempfile
import sqlite3
from pathlib import Path
from datetime import datetime
from PIL import Image, ImageTk, ImageEnhance

# --- Constants ---
DEFAULT_ZOOM = 2.5
MIN_ZOOM = 0.5
MAX_ZOOM = 4.0
ZOOM_STEP = 0.25
PAGES_PER_BATCH = 10
LOAD_THRESHOLD = 5  # Load more pages when reaching the 5th page of loaded batch
PAGE_UNLOAD_THRESHOLD = 20  # Unload pages more than this many pages away
PAGE_GAP = 10  # Pixels between pages
SENTENCE_ENDINGS = re.compile(r'(?<=[.!?])\s+')
DATA_DIR = Path.home() / ".local" / "pdfest"
DEFAULT_SIDEBAR_WIDTH = 300


class LibraryDB:
    """SQLite database manager for library and settings"""
    
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.db_path = DATA_DIR / "library.db"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_tables()
    
    def _init_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                title TEXT,
                total_pages INTEGER DEFAULT 0,
                last_page INTEGER DEFAULT 0,
                last_sentence INTEGER DEFAULT 0,
                zoom_level REAL DEFAULT 2.5,
                last_opened TEXT,
                thumbnail_path TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        # Add zoom_level column if it doesn't exist (migration)
        try:
            cursor.execute("ALTER TABLE books ADD COLUMN zoom_level REAL DEFAULT 2.5")
        except sqlite3.OperationalError:
            pass  # Column already exists
        
        # Add margin columns for per-book TTS exclusion (migration)
        try:
            cursor.execute("ALTER TABLE books ADD COLUMN header_margin REAL DEFAULT 50")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE books ADD COLUMN footer_margin REAL DEFAULT 60")
        except sqlite3.OperationalError:
            pass
        # Add column_mode for 1-col/2-col reading (migration)
        try:
            cursor.execute("ALTER TABLE books ADD COLUMN column_mode INTEGER DEFAULT 1")
        except sqlite3.OperationalError:
            pass
        self.conn.commit()
    
    def get_setting(self, key, default=None):
        cursor = self.conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row['value'] if row else default
    
    def set_setting(self, key, value):
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value))
        )
        self.conn.commit()
    
    def add_book(self, path, title=None, total_pages=0):
        if title is None:
            title = Path(path).stem
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO books (path, title, total_pages, last_opened)
            VALUES (?, ?, ?, ?)
        ''', (path, title, total_pages, datetime.now().isoformat()))
        self.conn.commit()
        return cursor.lastrowid
    
    def update_book_progress(self, path, last_page, last_sentence=0, zoom_level=None, header_margin=None, footer_margin=None, column_mode=None):
        cursor = self.conn.cursor()
        updates = ["last_page = ?", "last_sentence = ?", "last_opened = ?"]
        params = [last_page, last_sentence, datetime.now().isoformat()]
        
        if zoom_level is not None:
            updates.append("zoom_level = ?")
            params.append(zoom_level)
        if header_margin is not None:
            updates.append("header_margin = ?")
            params.append(header_margin)
        if footer_margin is not None:
            updates.append("footer_margin = ?")
            params.append(footer_margin)
        if column_mode is not None:
            updates.append("column_mode = ?")
            params.append(column_mode)
        
        params.append(path)
        cursor.execute(f"UPDATE books SET {', '.join(updates)} WHERE path = ?", params)
        self.conn.commit()
    
    def get_book(self, path):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM books WHERE path = ?", (path,))
        return cursor.fetchone()
    
    def get_all_books(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM books ORDER BY last_opened DESC")
        return cursor.fetchall()
    
    def get_last_opened_book(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM books ORDER BY last_opened DESC LIMIT 1")
        return cursor.fetchone()
    
    def remove_book(self, path):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM books WHERE path = ?", (path,))
        self.conn.commit()
    
    def close(self):
        self.conn.close()


class PDFSentence:
    """Struct to hold the text of a sentence and the list of rects (bounding boxes) for highlighting"""
    def __init__(self, text, rects, page_num, y_offset):
        self.text = text
        self.rects = rects  # List of (x0, y0, x1, y1) tuples in LOCAL page coords
        self.page_num = page_num
        self.y_offset = y_offset  # Global Y offset of the page this sentence is on


class VisualEdgeReader:
    def __init__(self, root):
        self.root = root
        self.root.title("PDFest")
        self.root.geometry("1200x800")
        self.root.configure(bg="#333")
        
        # Initialize database
        self.db = LibraryDB()
        self.current_pdf_path = None

        # State
        self.doc = None
        self.zoom_level = DEFAULT_ZOOM
        self.loaded_pages = set()  # Which pages have been rendered
        self.page_images = {}  # page_num -> PhotoImage (keep references to prevent GC)
        self.page_pil_images = {}  # page_num -> PIL Image (original without highlight)
        self.page_offsets = {}  # page_num -> Y offset on canvas
        self.page_heights = {}  # page_num -> height at current zoom
        self.page_width = 0  # Width of pages at current zoom
        self.sentences = []  # List of PDFSentence objects (global across loaded pages)
        self.current_sentence_idx = 0
        self.last_highlighted_page = None  # Track which page has highlight for cleanup
        self._is_playing = False  # Backing field for property
        self.stop_signal = False
        self.audio_file = os.path.join(tempfile.gettempdir(), "edge_tts_stream.mp3")
        
        # Load saved voice or use default
        saved_voice = self.db.get_setting("tts_voice", "en-US-AndrewMultilingualNeural")
        self.voice = saved_voice
        
        self.total_pages = 0
        self.estimated_page_height = 800  # Will be updated after first page render
        self.canvas_height = 0
        self.loading_lock = threading.Lock()
        self.is_loading = False
        
        # Audio caching
        self.audio_cache = {}  # sentence_idx -> audio file path
        self.cache_lock = threading.Lock()
        self.cache_ahead = 5  # Number of sentences to pre-cache ahead
        self.cache_worker_running = False
        self.playback_generation = 0  # Incremented on skip to invalidate stale audio
        self.pending_restart = False  # Track if we should restart after skip spam
        self.restart_after_id = None  # ID for scheduled restart
        
        # TOC data
        self.toc = []  # List of (level, title, page_num) tuples
        self.sidebar_visible = True
        
        # Get saved sidebar width
        saved_width = self.db.get_setting("sidebar_width", DEFAULT_SIDEBAR_WIDTH)
        self.sidebar_width = int(saved_width)
        
        # Sidebar resize state
        self.sidebar_resizing = False
        
        # Text selection state
        self.selection_start = None  # (canvas_x, canvas_y)
        self.selection_end = None
        self.selected_text = ""
        self.selection_rects = []  # List of (page_num, x0, y0, x1, y1) for drawing
        
        # Header/footer margins for TTS (in points, at 1x zoom) - loaded per-book
        self.header_margin = 0.0
        self.footer_margin = 0.0
        
        # Column mode (1=single column, 2=two columns) - loaded per-book
        self.column_mode = 1
        
        # Brightness filter (0.0-1.0, where 1.0 is normal, lower is dimmer)
        self.brightness = float(self.db.get_setting("brightness", 1.0))

        # Init Audio
        pygame.mixer.init()

        # --- UI Layout ---
        
        # Toolbar
        toolbar = tk.Frame(root, bg="#2d2d30", pady=5)
        toolbar.pack(fill=tk.X)

        ttk.Button(toolbar, text="📚 Library", command=self.show_library).pack(side=tk.LEFT, padx=5)
        ttk.Button(toolbar, text="Open PDF", command=self.open_pdf).pack(side=tk.LEFT, padx=5)
        
        # Sidebar toggle
        ttk.Button(toolbar, text="☰ TOC", command=self.toggle_sidebar).pack(side=tk.LEFT, padx=5)
        
        # Margin settings (for TTS exclusion)
        ttk.Button(toolbar, text="📏 Margins", command=self.show_margin_settings).pack(side=tk.LEFT, padx=5)
        
        # Brightness control
        ttk.Button(toolbar, text="🌙 Dim", command=self.show_brightness_settings).pack(side=tk.LEFT, padx=5)
        
        # Column mode toggle (1-col / 2-col)
        self.btn_column_mode = ttk.Button(toolbar, text="📄 1-Col", command=self.toggle_column_mode)
        self.btn_column_mode.pack(side=tk.LEFT, padx=5)

        # Playback controls (right side)
        self.btn_play = ttk.Button(toolbar, text="▶ Play", command=self.toggle_play)
        self.btn_play.pack(side=tk.RIGHT, padx=10)
        ttk.Button(toolbar, text=">> Next Sent", command=self.next_sentence).pack(side=tk.RIGHT)
        ttk.Button(toolbar, text="<< Prev Sent", command=self.prev_sentence).pack(side=tk.RIGHT)
        
        # Voice selection (right side, next to playback)
        ttk.Button(toolbar, text="🔊 Voice", command=self.show_voice_settings).pack(side=tk.RIGHT, padx=5)
        
        # Page navigation with zoom - centered
        page_frame = tk.Frame(toolbar, bg="#2d2d30")
        page_frame.pack(expand=True)  # Center by using expand
        
        # Zoom controls (left of page number)
        ttk.Button(page_frame, text="−", width=3, command=self.zoom_out).pack(side=tk.LEFT, padx=2)
        self.lbl_zoom = ttk.Label(page_frame, text=f"{int(self.zoom_level * 100)}%", 
                                   background="#2d2d30", foreground="white", width=5)
        self.lbl_zoom.pack(side=tk.LEFT, padx=2)
        ttk.Button(page_frame, text="+", width=3, command=self.zoom_in).pack(side=tk.LEFT, padx=(2, 15))
        
        # Page entry
        self.page_entry = tk.Entry(page_frame, width=4, bg="#3d3d3d", fg="white", 
                                   insertbackground="white", relief=tk.FLAT,
                                   font=("Arial", 10), justify="center")
        self.page_entry.pack(side=tk.LEFT, ipady=5)
        self.page_entry.insert(0, "1")
        self.page_entry.bind("<Return>", self.on_page_entry_confirm)
        self.page_entry.bind("<FocusIn>", self.on_page_entry_focus)
        self.page_entry.bind("<FocusOut>", self.on_page_entry_blur)
        
        self.lbl_total_pages = tk.Label(page_frame, text=" of 0", bg="#2d2d30", fg="white",
                                        font=("Arial", 10))
        self.lbl_total_pages.pack(side=tk.LEFT)
        
        # Track the page value before editing
        self.page_entry_original = "1"

        # Main content area (sidebar + canvas)
        self.main_frame = tk.Frame(root, bg="#555")
        self.main_frame.pack(fill=tk.BOTH, expand=True)
        
        # Sidebar for TOC/Pages
        self.sidebar = tk.Frame(self.main_frame, bg="#2d2d30", width=self.sidebar_width)
        self.sidebar.pack(side=tk.LEFT, fill=tk.Y)
        self.sidebar.pack_propagate(False)  # Maintain fixed width
        
        # Sidebar header
        sidebar_header = tk.Frame(self.sidebar, bg="#1e1e1e")
        sidebar_header.pack(fill=tk.X)
        tk.Label(sidebar_header, text="Contents", bg="#1e1e1e", fg="white", 
                font=("Arial", 10, "bold"), pady=5).pack(side=tk.LEFT, padx=10)
        
        # Sidebar listbox with scrollbar
        sidebar_scroll = tk.Scrollbar(self.sidebar)
        sidebar_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Add a frame for padding
        toc_frame = tk.Frame(self.sidebar, bg="#2d2d30", padx=20, pady=5)
        toc_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        self.toc_listbox = tk.Listbox(toc_frame, bg="#2d2d30", fg="white",
                                       selectbackground="#0078d7", selectforeground="white",
                                       borderwidth=0, highlightthickness=0,
                                       yscrollcommand=sidebar_scroll.set,
                                       font=("Arial", 13))
        self.toc_listbox.pack(fill=tk.BOTH, expand=True)
        sidebar_scroll.config(command=self.toc_listbox.yview)
        self.toc_listbox.bind("<<ListboxSelect>>", self.on_toc_select)
        
        # Resize handle between sidebar and canvas
        self.resize_handle = tk.Frame(self.main_frame, bg="#444", width=5, cursor="sb_h_double_arrow")
        self.resize_handle.pack(side=tk.LEFT, fill=tk.Y)
        self.resize_handle.bind("<Button-1>", self.start_sidebar_resize)
        self.resize_handle.bind("<B1-Motion>", self.do_sidebar_resize)
        self.resize_handle.bind("<ButtonRelease-1>", self.end_sidebar_resize)

        # Canvas area (Scrollable)
        self.canvas_frame = tk.Frame(self.main_frame, bg="#555")
        self.canvas_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.v_scroll = tk.Scrollbar(self.canvas_frame, orient=tk.VERTICAL)
        self.h_scroll = tk.Scrollbar(self.canvas_frame, orient=tk.HORIZONTAL)

        self.canvas = tk.Canvas(self.canvas_frame, bg="#1a1a1a", 
                                yscrollcommand=self.v_scroll.set, 
                                xscrollcommand=self.h_scroll.set)
        
        self.v_scroll.config(command=self.canvas.yview)
        self.h_scroll.config(command=self.canvas.xview)

        self.v_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.h_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Bind scroll events for lazy loading and page tracking
        self.canvas.bind("<Configure>", self.on_canvas_configure)
        self.v_scroll.bind("<ButtonRelease-1>", self.on_scroll)
        self.canvas.bind("<MouseWheel>", self.on_mousewheel)
        self.canvas.bind("<Button-4>", self.on_mousewheel)  # Linux scroll up
        self.canvas.bind("<Button-5>", self.on_mousewheel)  # Linux scroll down
        
        # Text selection bindings
        self.canvas.bind("<ButtonPress-1>", self.on_selection_start)
        self.canvas.bind("<B1-Motion>", self.on_selection_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_selection_end)
        
        # Copy shortcut
        self.root.bind("<Control-c>", self.copy_selection)
        
        # Keyboard shortcuts for TTS and navigation
        self.root.bind("<p>", lambda e: self.toggle_play())
        self.root.bind("<space>", lambda e: self.toggle_play())
        self.root.bind("<j>", lambda e: self.scroll_down())
        self.root.bind("<k>", lambda e: self.scroll_up())
        self.root.bind("<h>", lambda e: self.prev_sentence())
        self.root.bind("<l>", lambda e: self.next_sentence())
        self.root.bind("<t>", lambda e: self.toggle_sidebar())
        self.root.bind("<o>", lambda e: self.show_library())

    # --- Property for is_playing with auto UI update ---
    @property
    def is_playing(self):
        return self._is_playing
    
    @is_playing.setter
    def is_playing(self, value):
        self._is_playing = value
        # Auto-update button text if button exists
        if hasattr(self, 'btn_play'):
            if value:
                self.btn_play.config(text="⏸ Pause")
            else:
                self.btn_play.config(text="▶ Play")

    def open_pdf(self, filename=None):
        if filename is None:
            filename = filedialog.askopenfilename(filetypes=[("PDF Files", "*.pdf")])
        if not filename or not os.path.exists(filename):
            return
        
        # Save current book progress before switching
        self.save_current_progress()
        
        # Reset state
        self.canvas.delete("all")
        self.loaded_pages.clear()
        self.page_images.clear()
        self.page_pil_images.clear()
        self.page_offsets.clear()
        self.page_heights.clear()
        self.sentences.clear()
        self.audio_cache.clear()
        self.current_sentence_idx = 0
        self.is_playing = False
        self.stop_signal = True
        
        self.doc = fitz.open(filename)
        self.total_pages = len(self.doc)
        self.current_pdf_path = filename
        
        # Add to library database
        self.db.add_book(filename, total_pages=self.total_pages)
        
        # Load TOC
        self.load_toc()
        
        # Check for saved zoom level and margins before rendering
        book_info = self.db.get_book(filename)
        if book_info:
            if book_info['zoom_level']:
                self.zoom_level = book_info['zoom_level']
                self.lbl_zoom.config(text=f"{int(self.zoom_level * 100)}%")
            # Load per-book margins (default 50/60 for new books)
            self.header_margin = float(book_info['header_margin'] if book_info['header_margin'] is not None else 50)
            self.footer_margin = float(book_info['footer_margin'] if book_info['footer_margin'] is not None else 60)
            # Load column mode
            self.column_mode = int(book_info['column_mode'] if book_info['column_mode'] is not None else 1)
            self._update_column_button()
        else:
            self.header_margin = 50.0
            self.footer_margin = 60.0
            self.column_mode = 1
            self._update_column_button()
        
        # Estimate total canvas height based on first page
        first_page = self.doc.load_page(0)
        rect = first_page.rect
        self.page_width = int(rect.width * self.zoom_level)
        self.estimated_page_height = int(rect.height * self.zoom_level) + PAGE_GAP
        self.canvas_height = self.total_pages * self.estimated_page_height
        
        # Set scroll region - width will be adjusted for centering
        self.canvas.config(scrollregion=(0, 0, max(self.page_width, self.canvas.winfo_width()), self.canvas_height))
        
        # Load initial batch of pages
        self.render_pages(0, PAGES_PER_BATCH)
        
        # Restore last reading position or start at page 0
        if book_info and book_info['last_page'] > 0:
            self.scroll_to_page(book_info['last_page'])
            self.current_sentence_idx = book_info['last_sentence'] or 0
        else:
            # New book - start at page 0
            self.canvas.yview_moveto(0)
        
        self.update_page_indicator()
        self.root.title(f"Visual Edge Reader - {Path(filename).name}")
    
    def load_toc(self):
        """Load table of contents from PDF or generate page list"""
        self.toc_listbox.delete(0, tk.END)
        self.toc = []
        
        # Try to get TOC from PDF
        toc_data = self.doc.get_toc()
        
        if toc_data:
            # PDF has a table of contents
            for item in toc_data:
                level, title, page_num = item[0], item[1], item[2]
                # Indent based on level
                indent = "  " * (level - 1)
                display_text = f"{indent}{title}"
                self.toc.append((level, title, page_num - 1))  # Convert to 0-indexed
                self.toc_listbox.insert(tk.END, display_text)
        else:
            # No TOC, show page list
            for i in range(self.total_pages):
                self.toc.append((1, f"Page {i + 1}", i))
                self.toc_listbox.insert(tk.END, f"Page {i + 1}")
    
    def toggle_sidebar(self):
        """Toggle sidebar visibility"""
        if self.sidebar_visible:
            self.sidebar.pack_forget()
            self.resize_handle.pack_forget()
            self.sidebar_visible = False
        else:
            # Re-pack sidebar and handle before canvas
            self.canvas_frame.pack_forget()
            self.sidebar.pack(side=tk.LEFT, fill=tk.Y)
            self.resize_handle.pack(side=tk.LEFT, fill=tk.Y)
            self.canvas_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            self.sidebar_visible = True
    
    def on_toc_select(self, event):
        """Handle TOC item selection"""
        selection = self.toc_listbox.curselection()
        if not selection:
            return
        
        idx = selection[0]
        if idx < len(self.toc):
            _, _, page_num = self.toc[idx]
            self.scroll_to_page(page_num)

        # Clear selection so it doesn't stay highlighted
        self.toc_listbox.selection_clear(0, tk.END)
        self.btn_play.focus_set()
        
    
    def goto_page(self, event=None):
        """Jump to the page number entered in the entry field"""
        try:
            page_num = int(self.page_entry.get()) - 1  # Convert to 0-indexed
            if 0 <= page_num < self.total_pages:
                self.scroll_to_page(page_num)
                self.page_entry_original = str(page_num + 1)
            else:
                # Invalid - revert to original
                self.page_entry.delete(0, tk.END)
                self.page_entry.insert(0, self.page_entry_original)
        except ValueError:
            # Invalid - revert to original
            self.page_entry.delete(0, tk.END)
            self.page_entry.insert(0, self.page_entry_original)
    
    def on_page_entry_confirm(self, event=None):
        """Handle Enter key in page entry"""
        self.goto_page()
        self.canvas.focus_set()  # Remove focus from entry
    
    def on_page_entry_focus(self, event=None):
        """Save current value when entry gets focus"""
        self.page_entry_original = self.page_entry.get()
        self.page_entry.select_range(0, tk.END)
    
    def on_page_entry_blur(self, event=None):
        """Revert to original value when focus is lost without Enter"""
        # Always revert to what the current visible page is
        visible_page = self.get_visible_page()
        self.page_entry.delete(0, tk.END)
        self.page_entry.insert(0, str(visible_page + 1))
        self.page_entry_original = str(visible_page + 1)
    
    def reset_page_entry_focus(self):
        """Reset page entry if it has focus"""
        if self.root.focus_get() == self.page_entry:
            self.canvas.focus_set()
    
    def toggle_column_mode(self):
        """Toggle between 1-column and 2-column reading mode"""
        if self.column_mode == 1:
            self.column_mode = 2
        else:
            self.column_mode = 1
        
        self._update_column_button()
        
        # Save to database
        if self.current_pdf_path:
            self.db.update_book_progress(
                self.current_pdf_path,
                self.get_visible_page(),
                self.current_sentence_idx,
                column_mode=self.column_mode
            )
        
        # Re-analyze sentences with new column mode
        self.sentences.clear()
        for page_num in self.loaded_pages:
            page = self.doc.load_page(page_num)
            y_offset = self.page_offsets.get(page_num, page_num * self.estimated_page_height)
            self.analyze_page_sentences(page, page_num, y_offset)
        
        self.current_sentence_idx = 0
    
    def _update_column_button(self):
        """Update column mode button text"""
        if self.column_mode == 2:
            self.btn_column_mode.config(text="📑 2-Col")
        else:
            self.btn_column_mode.config(text="📄 1-Col")

    def scroll_to_page(self, page_num):
        """Scroll the canvas to show a specific page"""
        if not self.doc or page_num < 0 or page_num >= self.total_pages:
            return
        
        # Load pages around the target (both before and after)
        start_page = max(0, page_num - PAGES_PER_BATCH)
        end_page = min(self.total_pages, page_num + PAGES_PER_BATCH)
        
        for p in range(start_page, end_page):
            if p not in self.loaded_pages:
                self.render_single_page(p)
        
        # Calculate scroll position
        y_offset = page_num * self.estimated_page_height
        scroll_pos = y_offset / self.canvas_height
        self.canvas.yview_moveto(scroll_pos)
        
        self.update_page_indicator()

    def render_pages(self, start_page, count):
        """Render a batch of pages starting from start_page"""
        if not self.doc:
            return
        
        end_page = min(start_page + count, self.total_pages)
        
        for page_num in range(start_page, end_page):
            if page_num in self.loaded_pages:
                continue
            
            self.render_single_page(page_num)
        
        self.is_loading = False

    def render_single_page(self, page_num):
        """Render a single page and add it to the canvas"""
        if page_num in self.loaded_pages:
            return
        
        page = self.doc.load_page(page_num)
        mat = fitz.Matrix(self.zoom_level, self.zoom_level)
        pix = page.get_pixmap(matrix=mat)
        
        # Calculate Y offset for this page
        y_offset = page_num * self.estimated_page_height
        self.page_offsets[page_num] = y_offset
        self.page_heights[page_num] = pix.height
        
        # Create image and store PIL version for highlighting
        img_data = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        self.page_pil_images[page_num] = img_data.copy()  # Store original (full brightness)
        
        # Apply brightness filter
        if self.brightness < 1.0:
            enhancer = ImageEnhance.Brightness(img_data)
            img_data = enhancer.enhance(self.brightness)
        
        photo = ImageTk.PhotoImage(img_data)
        self.page_images[page_num] = photo  # Keep reference
        
        # Calculate x offset for centering
        canvas_width = self.canvas.winfo_width()
        x_offset = max(0, (canvas_width - pix.width) // 2)
        
        # Draw on canvas (centered)
        self.canvas.create_image(x_offset, y_offset, image=photo, anchor=tk.NW, tags=f"page_{page_num}")
        
        # Mark as loaded and analyze sentences
        self.loaded_pages.add(page_num)
        self.analyze_page_sentences(page, page_num, y_offset)

    def analyze_page_sentences(self, page, page_num, y_offset):
        """Extracts words and groups them into sentences with coordinates"""
        words = page.get_text("words")
        page_height = page.rect.height
        page_width = page.rect.width
        
        # Filter out header/footer words
        filtered_words = []
        for w in words:
            word_y = w[1]
            if self.header_margin > 0 and word_y < self.header_margin:
                continue
            if self.footer_margin > 0 and word_y > (page_height - self.footer_margin):
                continue
            filtered_words.append(w)
        
        # For 2-column mode, reorder words: left column first, then right column
        if self.column_mode == 2:
            midpoint = page_width / 2
            left_words = [w for w in filtered_words if w[2] < midpoint]  # x1 < midpoint
            right_words = [w for w in filtered_words if w[0] >= midpoint]  # x0 >= midpoint
            # Sort each column by Y position (top to bottom), then X
            left_words.sort(key=lambda w: (w[1], w[0]))
            right_words.sort(key=lambda w: (w[1], w[0]))
            # Process left column first, then right
            filtered_words = left_words + right_words
        
        current_text = []
        current_rects = []
        
        for w in filtered_words:
            # Scale coordinates to match our Zoom level
            rect = (w[0] * self.zoom_level, w[1] * self.zoom_level, 
                    w[2] * self.zoom_level, w[3] * self.zoom_level)
            text = w[4]
            
            current_text.append(text)
            current_rects.append(rect)
            
            # Check for sentence ending
            if re.search(r'[.!?]$', text):
                full_sentence = " ".join(current_text)
                self.sentences.append(PDFSentence(full_sentence, current_rects, page_num, y_offset))
                current_text = []
                current_rects = []

        # Add any remaining text as a sentence
        if current_text:
            self.sentences.append(PDFSentence(" ".join(current_text), current_rects, page_num, y_offset))
        
        # Sort sentences by page and position
        # For 2-column mode, we need to sort by column first (X position), then Y
        if self.column_mode == 2:
            page_width = page.rect.width * self.zoom_level
            midpoint = page_width / 2
            # Sort by: page, column (left=0, right=1), then Y position
            self.sentences.sort(key=lambda s: (
                s.page_num,
                0 if (s.rects and s.rects[0][0] < midpoint) else 1,  # left=0, right=1
                s.rects[0][1] if s.rects else 0  # Y position within column
            ))
        else:
            self.sentences.sort(key=lambda s: (s.page_num, s.rects[0][1] if s.rects else 0))

    def get_visible_page(self):
        """Determine which page is currently most visible"""
        if not self.doc:
            return 0
        
        # Get current scroll position
        scroll_top = self.canvas.canvasy(0)
        canvas_height = self.canvas.winfo_height()
        scroll_center = scroll_top + canvas_height / 2
        
        # Find which page contains the center of the viewport
        for page_num in range(self.total_pages):
            page_top = page_num * self.estimated_page_height
            page_bottom = page_top + self.estimated_page_height
            if page_top <= scroll_center < page_bottom:
                return page_num
        
        return 0

    def check_and_load_more_pages(self):
        """Check if we need to load more pages based on scroll position"""
        if self.is_loading or not self.doc:
            return
        
        visible_page = self.get_visible_page()
        
        # Find loaded page range
        min_loaded = min(self.loaded_pages) if self.loaded_pages else self.total_pages
        max_loaded = max(self.loaded_pages) if self.loaded_pages else -1
        
        pages_to_load = []
        
        # Check if we need to load pages ahead
        if visible_page >= max_loaded - LOAD_THRESHOLD + 1:
            next_start = max_loaded + 1
            for p in range(next_start, min(next_start + PAGES_PER_BATCH, self.total_pages)):
                if p not in self.loaded_pages:
                    pages_to_load.append(p)
        
        # Check if we need to load pages behind
        if visible_page <= min_loaded + LOAD_THRESHOLD - 1:
            prev_end = min_loaded
            for p in range(max(0, prev_end - PAGES_PER_BATCH), prev_end):
                if p not in self.loaded_pages:
                    pages_to_load.append(p)
        
        if pages_to_load:
            with self.loading_lock:
                if not self.is_loading:
                    self.is_loading = True
                    self.root.after(10, lambda: self._load_pages_list(pages_to_load))
    
    def _load_pages_list(self, pages):
        """Load a specific list of pages"""
        for page_num in pages:
            if page_num not in self.loaded_pages:
                self.render_single_page(page_num)
        self.is_loading = False
        
        # Unload distant pages to save memory
        self.unload_distant_pages()
    
    def unload_distant_pages(self):
        """Unload pages that are far from the current view to save memory"""
        if not self.doc or not self.loaded_pages:
            return
        
        visible_page = self.get_visible_page()
        pages_to_unload = []
        
        for page_num in list(self.loaded_pages):
            distance = abs(page_num - visible_page)
            if distance > PAGE_UNLOAD_THRESHOLD:
                pages_to_unload.append(page_num)
        
        for page_num in pages_to_unload:
            # Remove from canvas
            self.canvas.delete(f"page_{page_num}")
            # Remove from tracking
            self.loaded_pages.discard(page_num)
            # Free memory
            if page_num in self.page_images:
                del self.page_images[page_num]
            if page_num in self.page_pil_images:
                del self.page_pil_images[page_num]
            if page_num in self.page_offsets:
                del self.page_offsets[page_num]
            if page_num in self.page_heights:
                del self.page_heights[page_num]

    def update_page_indicator(self):
        """Update the page entry and total pages label based on visible page"""
        if not self.doc:
            return
        visible_page = self.get_visible_page()
        
        # Only update if entry doesn't have focus (user might be typing)
        if self.root.focus_get() != self.page_entry:
            self.page_entry.delete(0, tk.END)
            self.page_entry.insert(0, str(visible_page + 1))
            self.page_entry_original = str(visible_page + 1)
        
        self.lbl_total_pages.config(text=f" of {self.total_pages}")

    def on_scroll(self, event=None):
        """Handle scroll events"""
        self.update_page_indicator()
        self.check_and_load_more_pages()
    
    def scroll_down(self):
        """Scroll down - for keyboard shortcut"""
        self.reset_page_entry_focus()
        self.smooth_scroll(120)
    
    def scroll_up(self):
        """Scroll up - for keyboard shortcut"""
        self.reset_page_entry_focus()
        self.smooth_scroll(-120)

    def on_mousewheel(self, event):
        """Handle mouse wheel scrolling with smooth animation"""
        # Calculate scroll amount in pixels
        if event.num == 4:  # Linux scroll up
            delta = -60
        elif event.num == 5:  # Linux scroll down
            delta = 60
        else:  # Windows/Mac
            delta = -1 * (event.delta // 2)
        
        # Reset page entry if focused
        self.reset_page_entry_focus()
        
        # Start smooth scroll animation
        self.smooth_scroll(delta)
    
    def smooth_scroll(self, total_delta, steps=8):
        """Animate scroll over multiple frames for smoothness"""
        if not hasattr(self, '_scroll_queue'):
            self._scroll_queue = 0
            self._scroll_animating = False
        
        self._scroll_queue += total_delta
        
        if not self._scroll_animating:
            self._scroll_animating = True
            self._animate_scroll(steps)
    
    def _animate_scroll(self, steps_remaining):
        """Execute one frame of scroll animation"""
        if not hasattr(self, '_scroll_queue') or abs(self._scroll_queue) < 1:
            self._scroll_animating = False
            self._scroll_queue = 0
            self.on_scroll()
            return
        
        # Calculate step size with easing
        step = self._scroll_queue / max(steps_remaining, 3)
        
        # Apply scroll
        current_pos = self.canvas.yview()[0]
        scroll_fraction = step / self.canvas_height if self.canvas_height > 0 else 0
        new_pos = max(0, min(1, current_pos + scroll_fraction))
        self.canvas.yview_moveto(new_pos)
        
        self._scroll_queue -= step
        
        # Continue animation
        if abs(self._scroll_queue) > 0.5:
            self.root.after(12, lambda: self._animate_scroll(steps_remaining - 1))  # ~83fps
        else:
            self._scroll_animating = False
            self._scroll_queue = 0
            self.on_scroll()

    def on_canvas_configure(self, event):
        """Handle canvas resize - re-center pages"""
        if self.doc and self.loaded_pages:
            # Save scroll position
            scroll_pos = self.canvas.yview()[0]
            
            # Re-render to adjust centering
            self.reposition_pages()
            
            # Restore scroll position
            self.canvas.yview_moveto(scroll_pos)
        self.on_scroll()
    
    def reposition_pages(self):
        """Reposition all loaded pages to center them"""
        canvas_width = self.canvas.winfo_width()
        x_offset = max(0, (canvas_width - self.page_width) // 2)
        
        for page_num in self.loaded_pages:
            y_offset = self.page_offsets.get(page_num, page_num * self.estimated_page_height)
            # Delete and recreate the page image at new position
            self.canvas.delete(f"page_{page_num}")
            if page_num in self.page_images:
                self.canvas.create_image(x_offset, y_offset, image=self.page_images[page_num], 
                                        anchor=tk.NW, tags=f"page_{page_num}")

    # --- Text selection ---
    def on_selection_start(self, event):
        """Start text selection"""
        # Reset page entry if focused
        self.reset_page_entry_focus()
        
        # Clear previous selection
        self.canvas.delete("selection")
        self.selection_rects = []
        self.selected_text = ""
        
        # Record start position in canvas coordinates
        canvas_x = self.canvas.canvasx(event.x)
        canvas_y = self.canvas.canvasy(event.y)
        self.selection_start = (canvas_x, canvas_y)
        self.selection_end = None
    
    def on_selection_drag(self, event):
        """Handle selection drag"""
        if self.selection_start is None:
            return
        
        canvas_x = self.canvas.canvasx(event.x)
        canvas_y = self.canvas.canvasy(event.y)
        self.selection_end = (canvas_x, canvas_y)
        
        # Draw selection rectangle (temporary visual feedback)
        self.canvas.delete("selection_rect")
        x0, y0 = self.selection_start
        x1, y1 = self.selection_end
        self.canvas.create_rectangle(
            min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1),
            fill="#4488ff", stipple="gray50", outline="#2266dd",
            tags="selection_rect"
        )
    
    def on_selection_end(self, event):
        """End selection and extract text, or handle link click"""
        if self.selection_start is None:
            return
        
        canvas_x = self.canvas.canvasx(event.x)
        canvas_y = self.canvas.canvasy(event.y)
        self.selection_end = (canvas_x, canvas_y)
        
        # Remove temporary rectangle
        self.canvas.delete("selection_rect")
        
        # Only process if there was meaningful drag
        x0, y0 = self.selection_start
        x1, y1 = self.selection_end
        if abs(x1 - x0) < 5 and abs(y1 - y0) < 5:
            # This was a click, not a drag - check for links
            self.check_and_open_link(canvas_x, canvas_y)
            self.selection_start = None
            return
        
        # Get selected text and draw proper highlight
        self.extract_and_highlight_selection()
    
    def check_and_open_link(self, canvas_x, canvas_y):
        """Check if click is on a link and open it"""
        if not self.doc:
            return
        
        # Calculate x offset for centering
        canvas_width = self.canvas.winfo_width()
        x_offset = max(0, (canvas_width - self.page_width) // 2)
        
        # Find which page was clicked
        for page_num in self.loaded_pages:
            page_y_offset = self.page_offsets.get(page_num, 0)
            page_height = self.page_heights.get(page_num, self.estimated_page_height)
            
            if canvas_y < page_y_offset or canvas_y > page_y_offset + page_height:
                continue
            
            # Convert to page coordinates
            page_x = (canvas_x - x_offset) / self.zoom_level
            page_y = (canvas_y - page_y_offset) / self.zoom_level
            
            # Get links on this page
            page = self.doc.load_page(page_num)
            links = page.get_links()
            
            for link in links:
                rect = link.get("from")
                if rect and rect.x0 <= page_x <= rect.x1 and rect.y0 <= page_y <= rect.y1:
                    # Found a link!
                    uri = link.get("uri")
                    if uri:
                        import webbrowser
                        webbrowser.open(uri)
                        return
                    
                    # Internal link (page jump)
                    dest_page = link.get("page")
                    if dest_page is not None and dest_page >= 0:
                        self.scroll_to_page(dest_page)
                        return
            break  # Only check the clicked page
    
    def extract_and_highlight_selection(self):
        """Extract text from selection area and draw highlights"""
        if not self.doc or self.selection_start is None or self.selection_end is None:
            return
        
        x0, y0 = self.selection_start
        x1, y1 = self.selection_end
        
        # Normalize coordinates
        sel_left = min(x0, x1)
        sel_right = max(x0, x1)
        sel_top = min(y0, y1)
        sel_bottom = max(y0, y1)
        
        # Calculate x offset for centering
        canvas_width = self.canvas.winfo_width()
        x_offset = max(0, (canvas_width - self.page_width) // 2)
        
        selected_texts = []
        self.canvas.delete("selection")
        
        # Find pages that intersect with selection
        for page_num in self.loaded_pages:
            page_y_offset = self.page_offsets.get(page_num, 0)
            page_height = self.page_heights.get(page_num, self.estimated_page_height)
            
            page_top = page_y_offset
            page_bottom = page_y_offset + page_height
            
            # Check if selection intersects this page
            if sel_bottom < page_top or sel_top > page_bottom:
                continue
            
            # Convert canvas coords to page coords
            page_sel_left = (sel_left - x_offset) / self.zoom_level
            page_sel_right = (sel_right - x_offset) / self.zoom_level
            page_sel_top = (sel_top - page_y_offset) / self.zoom_level
            page_sel_bottom = (sel_bottom - page_y_offset) / self.zoom_level
            
            # Get text from this page in the selection area
            page = self.doc.load_page(page_num)
            rect = fitz.Rect(page_sel_left, page_sel_top, page_sel_right, page_sel_bottom)
            
            # Get text blocks in selection
            text = page.get_text("text", clip=rect)
            if text.strip():
                selected_texts.append(text)
            
            # Get word rectangles for highlighting
            words = page.get_text("words", clip=rect)
            for word in words:
                # word is (x0, y0, x1, y1, "word", block_no, line_no, word_no)
                wx0, wy0, wx1, wy1 = word[:4]
                
                # Convert back to canvas coords
                canvas_wx0 = x_offset + wx0 * self.zoom_level
                canvas_wy0 = page_y_offset + wy0 * self.zoom_level
                canvas_wx1 = x_offset + wx1 * self.zoom_level
                canvas_wy1 = page_y_offset + wy1 * self.zoom_level
                
                # Draw blue highlight
                self.canvas.create_rectangle(
                    canvas_wx0, canvas_wy0, canvas_wx1, canvas_wy1,
                    fill="#4488ff", stipple="gray50", outline="",
                    tags="selection"
                )
        
        self.selected_text = "\n".join(selected_texts)
        self.selection_start = None
    
    def copy_selection(self, event=None):
        """Copy selected text to clipboard"""
        if self.selected_text:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.selected_text)
            # Brief visual feedback could be added here


    # --- Zoom functionality ---
    def zoom_in(self):
        if self.zoom_level < MAX_ZOOM:
            self.set_zoom(self.zoom_level + ZOOM_STEP)

    def zoom_out(self):
        if self.zoom_level > MIN_ZOOM:
            self.set_zoom(self.zoom_level - ZOOM_STEP)

    def set_zoom(self, new_zoom):
        """Change zoom level and re-render visible pages"""
        if not self.doc:
            return
        
        # Save relative scroll position
        old_scroll_fraction = self.canvas.yview()[0]
        
        # Update zoom
        self.zoom_level = new_zoom
        self.lbl_zoom.config(text=f"{int(self.zoom_level * 100)}%")
        
        # Recalculate page dimensions
        first_page = self.doc.load_page(0)
        rect = first_page.rect
        self.page_width = int(rect.width * self.zoom_level)
        self.estimated_page_height = int(rect.height * self.zoom_level) + PAGE_GAP
        self.canvas_height = self.total_pages * self.estimated_page_height
        
        # Clear and re-render
        self.canvas.delete("all")
        old_loaded = list(self.loaded_pages)
        self.loaded_pages.clear()
        self.page_images.clear()
        self.page_offsets.clear()
        self.page_heights.clear()
        self.sentences.clear()
        
        # Update scroll region
        self.canvas.config(scrollregion=(0, 0, max(self.page_width, self.canvas.winfo_width()), self.canvas_height))
        
        # Re-render previously loaded pages
        for page_num in old_loaded:
            self.render_single_page(page_num)
        
        # Restore scroll position
        self.canvas.yview_moveto(old_scroll_fraction)
        
        # Redraw highlight if playing
        if self.is_playing:
            self.draw_highlight()

    # --- Highlight drawing ---
    def draw_highlight(self):
        if not self.sentences or self.current_sentence_idx >= len(self.sentences):
            return

        sentence = self.sentences[self.current_sentence_idx]
        page_num = sentence.page_num
        
        # Clear highlight from previous page if different
        if self.last_highlighted_page is not None and self.last_highlighted_page != page_num:
            self.clear_highlight(self.last_highlighted_page)
        
        # Track current highlighted page
        self.last_highlighted_page = page_num
        
        # Make sure the page is loaded
        if page_num not in self.loaded_pages:
            self.render_single_page(page_num)
        
        y_offset = self.page_offsets.get(page_num, 0)
        
        # Auto-scroll only if the highlight is below 3/4 of the visible area
        if sentence.rects:
            first_rect = sentence.rects[0]
            global_y = y_offset + first_rect[1]
            
            # Get current visible area
            scroll_top = self.canvas.canvasy(0)
            canvas_visible_height = self.canvas.winfo_height()
            scroll_bottom = scroll_top + canvas_visible_height
            
            # Only scroll if sentence is below 3/4 of visible area or above visible area
            threshold = scroll_top + (canvas_visible_height * 0.75)
            
            if global_y > threshold or global_y < scroll_top:
                # Scroll to put sentence at about 1/5 from top
                target_scroll = global_y - (canvas_visible_height * 0.2)
                scroll_pos = max(0, target_scroll / self.canvas_height)
                self.canvas.yview_moveto(scroll_pos)

        # Get the original page image
        if page_num not in self.page_pil_images:
            return
        
        # Create a copy of the original image to draw on
        img = self.page_pil_images[page_num].copy().convert("RGBA")
        
        # Create highlight overlay
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        from PIL import ImageDraw
        draw = ImageDraw.Draw(overlay)
        
        # Group rects by their Y position (same line)
        lines = {}
        for r in sentence.rects:
            line_key = round(r[1] / 5) * 5
            if line_key not in lines:
                lines[line_key] = {'x0': r[0], 'y0': r[1], 'x1': r[2], 'y1': r[3]}
            else:
                lines[line_key]['x0'] = min(lines[line_key]['x0'], r[0])
                lines[line_key]['x1'] = max(lines[line_key]['x1'], r[2])
                lines[line_key]['y0'] = min(lines[line_key]['y0'], r[1])
                lines[line_key]['y1'] = max(lines[line_key]['y1'], r[3])
        
        # Draw semi-transparent yellow highlight for each line
        for line in lines.values():
            x0 = int(line['x0'] - 2)
            y0 = int(line['y0'] - 2)
            x1 = int(line['x1'] + 2)
            y1 = int(line['y1'] + 2)
            # Yellow with 40% opacity (102 out of 255)
            draw.rectangle([x0, y0, x1, y1], fill=(255, 255, 0, 102))
        
        # Composite the overlay onto the image
        highlighted_img = Image.alpha_composite(img, overlay).convert("RGB")
        
        # Apply brightness filter
        if self.brightness < 1.0:
            enhancer = ImageEnhance.Brightness(highlighted_img)
            highlighted_img = enhancer.enhance(self.brightness)
        
        # Update the page image on canvas
        photo = ImageTk.PhotoImage(highlighted_img)
        self.page_images[page_num] = photo  # Keep reference
        
        # Update canvas
        canvas_width = self.canvas.winfo_width()
        x_offset = max(0, (canvas_width - self.page_width) // 2)
        self.canvas.delete(f"page_{page_num}")
        self.canvas.create_image(x_offset, y_offset, image=photo, anchor=tk.NW, tags=f"page_{page_num}")
    
    def clear_highlight(self, page_num):
        """Restore original page image without highlight"""
        if page_num not in self.page_pil_images:
            return
        
        img = self.page_pil_images[page_num].copy()
        
        # Apply brightness filter
        if self.brightness < 1.0:
            enhancer = ImageEnhance.Brightness(img)
            img = enhancer.enhance(self.brightness)
        
        photo = ImageTk.PhotoImage(img)
        self.page_images[page_num] = photo
        
        y_offset = self.page_offsets.get(page_num, 0)
        canvas_width = self.canvas.winfo_width()
        x_offset = max(0, (canvas_width - self.page_width) // 2)
        
        self.canvas.delete(f"page_{page_num}")
        self.canvas.create_image(x_offset, y_offset, image=photo, anchor=tk.NW, tags=f"page_{page_num}")

    # --- Audio Logic ---
    def toggle_play(self):
        if self.is_playing:
            self.stop_signal = True
            self.is_playing = False
            pygame.mixer.music.stop()  # Stop audio immediately
        else:
            # Reset index if out of bounds
            if self.current_sentence_idx >= len(self.sentences):
                self.current_sentence_idx = 0
            
            # When starting playback, check if we should jump to visible page
            visible_page = self.get_visible_page()
            current_sentence = self.sentences[self.current_sentence_idx] if self.sentences else None
            
            # If current sentence is not on the visible page, find first sentence on visible page
            if current_sentence and current_sentence.page_num != visible_page:
                first_sentence_on_page = self.find_first_sentence_on_page(visible_page)
                if first_sentence_on_page is not None:
                    self.current_sentence_idx = first_sentence_on_page
            
            self.stop_signal = False
            self.is_playing = True
            threading.Thread(target=self.playback_loop, daemon=True).start()
    
    def find_first_sentence_on_page(self, page_num):
        """Find the index of the first sentence on the given page"""
        for i, sentence in enumerate(self.sentences):
            if sentence.page_num == page_num:
                return i
        return None

    async def generate_audio(self, text, output_file):
        communicate = edge_tts.Communicate(text, self.voice)
        await communicate.save(output_file)

    def run_async_generation(self, text, output_file):
        asyncio.run(self.generate_audio(text, output_file))
    
    def get_cache_file(self, sentence_idx):
        """Get the cache file path for a sentence"""
        return os.path.join(tempfile.gettempdir(), f"edge_tts_cache_{sentence_idx}.mp3")
    
    def cache_sentence(self, sentence_idx):
        """Generate and cache audio for a specific sentence"""
        if sentence_idx >= len(self.sentences):
            return False
        
        with self.cache_lock:
            if sentence_idx in self.audio_cache:
                return True  # Already cached
        
        sentence = self.sentences[sentence_idx]
        cache_file = self.get_cache_file(sentence_idx)
        
        try:
            self.run_async_generation(sentence.text, cache_file)
            with self.cache_lock:
                self.audio_cache[sentence_idx] = cache_file
            return True
        except Exception as e:
            print(f"Cache error for sentence {sentence_idx}: {e}")
            return False
    
    def start_cache_worker(self, start_idx):
        """Start background worker to cache upcoming sentences"""
        if self.cache_worker_running:
            return
        self.cache_worker_running = True
        threading.Thread(target=self._cache_worker, daemon=True).start()
    
    def _cache_worker(self):
        """Background worker that continuously caches upcoming sentences while playing"""
        try:
            while self.is_playing and not self.stop_signal:
                # Cache next 5 sentences from current position
                current = self.current_sentence_idx
                for i in range(current, min(current + self.cache_ahead, len(self.sentences))):
                    if self.stop_signal or not self.is_playing:
                        break
                    # Skip if already cached
                    with self.cache_lock:
                        if i in self.audio_cache:
                            continue
                    self.cache_sentence(i)
                
                # Small sleep to avoid busy loop
                if self.is_playing and not self.stop_signal:
                    import time
                    time.sleep(0.1)
        finally:
            self.cache_worker_running = False
    
    def clear_old_cache(self, current_idx):
        """Remove cached files for sentences we've passed"""
        with self.cache_lock:
            to_remove = [idx for idx in self.audio_cache if idx < current_idx - 1]
            for idx in to_remove:
                try:
                    if os.path.exists(self.audio_cache[idx]):
                        os.remove(self.audio_cache[idx])
                except:
                    pass
                del self.audio_cache[idx]

    def playback_loop(self):
        # Pre-cache first few sentences
        self.start_cache_worker(self.current_sentence_idx)
        
        # Track generation at start
        my_generation = self.playback_generation
        
        while self.current_sentence_idx < len(self.sentences) and not self.stop_signal:
            # Check if we've been invalidated by a skip
            if self.playback_generation != my_generation:
                break
            
            # Store the index we're about to play
            playing_idx = self.current_sentence_idx
            
            sentence = self.sentences[playing_idx]
            
            # Make sure the page containing this sentence is loaded
            if sentence.page_num not in self.loaded_pages:
                self.root.after(0, lambda pn=sentence.page_num: self.render_single_page(pn))
                pygame.time.wait(100)
            
            # UI Update
            self.root.after(0, self.draw_highlight)
            
            # Check if audio is cached, if not generate it now
            with self.cache_lock:
                cached_file = self.audio_cache.get(playing_idx)
            
            try:
                if cached_file and os.path.exists(cached_file):
                    # Use cached audio
                    pygame.mixer.music.load(cached_file)
                else:
                    # Generate on the fly (fallback)
                    self.run_async_generation(sentence.text, self.audio_file)
                    
                    # Check if invalidated during generation - skip if so
                    if self.playback_generation != my_generation or self.stop_signal:
                        continue
                    
                    pygame.mixer.music.load(self.audio_file)
                
                # Final check before playing - skip if invalidated
                if self.playback_generation != my_generation or self.stop_signal:
                    continue
                
                pygame.mixer.music.play()
                
                # Start caching next sentences while playing
                self.start_cache_worker(self.current_sentence_idx + 1)
                
            except Exception as e:
                error_msg = str(e)
                print(f"TTS Error: {e}")
                # Stop playback and show error message
                self.stop_signal = True
                self.root.after(0, self._set_is_playing_false)
                self.root.after(0, lambda: messagebox.showerror(
                    "TTS Error", 
                    f"Failed to generate audio.\n\nError: {error_msg}\n\nPlease check your internet connection."
                ))
                break

            while pygame.mixer.music.get_busy() and not self.stop_signal:
                # Also check generation during playback
                if self.playback_generation != my_generation:
                    pygame.mixer.music.stop()
                    break
                pygame.time.Clock().tick(10)

            if self.stop_signal or self.playback_generation != my_generation:
                break
            
            # Clean up old cache
            self.clear_old_cache(self.current_sentence_idx)
            
            self.current_sentence_idx += 1
            
            # Check if we need to load more pages for upcoming sentences
            if self.current_sentence_idx < len(self.sentences):
                next_sentence = self.sentences[self.current_sentence_idx]
                if next_sentence.page_num not in self.loaded_pages:
                    self.root.after(0, lambda: self.check_and_load_more_pages())
        
        self.stop_signal = True
        # Use root.after to update is_playing from UI thread (property will update button)
        self.root.after(0, self._set_is_playing_false)

    def _set_is_playing_false(self):
        """Helper to set is_playing from UI thread"""
        self.is_playing = False

    def next_sentence(self):
        if self.current_sentence_idx < len(self.sentences) - 1:
            if self.is_playing:
                self.stop_signal = True
                self.is_playing = False
                pygame.mixer.music.stop()
            
            self.playback_generation += 1
            self.current_sentence_idx += 1
            self.draw_highlight()

    def prev_sentence(self):
        if self.current_sentence_idx > 0:
            if self.is_playing:
                self.stop_signal = True
                self.is_playing = False
                pygame.mixer.music.stop()
            
            self.playback_generation += 1
            self.current_sentence_idx -= 1
            self.draw_highlight()
    
    # --- Sidebar resize methods ---
    def start_sidebar_resize(self, event):
        """Start resizing sidebar"""
        self.sidebar_resizing = True
        self.resize_start_x = event.x_root
        self.resize_start_width = self.sidebar_width
    
    def do_sidebar_resize(self, event):
        """Handle sidebar resize drag"""
        if not self.sidebar_resizing:
            return
        
        delta = event.x_root - self.resize_start_x
        new_width = max(150, min(600, self.resize_start_width + delta))
        self.sidebar_width = new_width
        self.sidebar.config(width=new_width)
    
    def end_sidebar_resize(self, event):
        """End sidebar resize and save width"""
        self.sidebar_resizing = False
        self.db.set_setting("sidebar_width", self.sidebar_width)
    
    # --- Margin settings ---
    def show_margin_settings(self):
        """Show dialog to configure header/footer margins for TTS"""
        margin_win = tk.Toplevel(self.root)
        margin_win.title("TTS Text Margins")
        margin_win.geometry("400x300")
        margin_win.configure(bg="#2d2d30")
        
        # Header
        tk.Label(margin_win, text="📏 Text Detection Margins", bg="#2d2d30", fg="white",
                font=("Arial", 14, "bold")).pack(pady=10)
        
        tk.Label(margin_win, text="Exclude header/footer regions from TTS reading", 
                bg="#2d2d30", fg="#aaa", font=("Arial", 10)).pack()
        
        # Header margin
        header_frame = tk.Frame(margin_win, bg="#2d2d30")
        header_frame.pack(fill=tk.X, padx=20, pady=10)
        tk.Label(header_frame, text="Header margin (pts):", bg="#2d2d30", fg="white",
                font=("Arial", 10)).pack(side=tk.LEFT)
        header_var = tk.DoubleVar(value=self.header_margin)
        header_scale = tk.Scale(header_frame, from_=0, to=150, orient=tk.HORIZONTAL,
                               variable=header_var, bg="#3d3d3d", fg="white",
                               highlightthickness=0, length=200)
        header_scale.pack(side=tk.RIGHT)
        header_scale.bind("<Motion>", lambda e: self.preview_margins(header_var.get(), footer_var.get()))
        
        # Footer margin
        footer_frame = tk.Frame(margin_win, bg="#2d2d30")
        footer_frame.pack(fill=tk.X, padx=20, pady=10)
        tk.Label(footer_frame, text="Footer margin (pts):", bg="#2d2d30", fg="white",
                font=("Arial", 10)).pack(side=tk.LEFT)
        footer_var = tk.DoubleVar(value=self.footer_margin)
        footer_scale = tk.Scale(footer_frame, from_=0, to=150, orient=tk.HORIZONTAL,
                               variable=footer_var, bg="#3d3d3d", fg="white",
                               highlightthickness=0, length=200)
        footer_scale.pack(side=tk.RIGHT)
        footer_scale.bind("<Motion>", lambda e: self.preview_margins(header_var.get(), footer_var.get()))
        
        # Buttons
        btn_frame = tk.Frame(margin_win, bg="#2d2d30")
        btn_frame.pack(pady=20)
        
        def apply_margins():
            self.header_margin = header_var.get()
            self.footer_margin = footer_var.get()
            # Save per-book margins
            if self.current_pdf_path:
                self.db.update_book_progress(
                    self.current_pdf_path,
                    self.get_visible_page(),
                    self.current_sentence_idx,
                    header_margin=self.header_margin,
                    footer_margin=self.footer_margin
                )
            # Re-analyze sentences for loaded pages
            self.sentences.clear()
            for page_num in self.loaded_pages:
                page = self.doc.load_page(page_num)
                y_offset = self.page_offsets.get(page_num, page_num * self.estimated_page_height)
                self.analyze_page_sentences(page, page_num, y_offset)
            self.canvas.delete("margin_preview")
            margin_win.destroy()
        
        def cancel():
            self.canvas.delete("margin_preview")
            margin_win.destroy()
        
        ttk.Button(btn_frame, text="Apply", command=apply_margins).pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_frame, text="Cancel", command=cancel).pack(side=tk.LEFT)
        
        # Clean up preview on window close
        margin_win.protocol("WM_DELETE_WINDOW", cancel)
    
    def preview_margins(self, header, footer):
        """Show visual preview of margin exclusion zones"""
        self.canvas.delete("margin_preview")
        
        if not self.doc or not self.loaded_pages:
            return
        
        canvas_width = self.canvas.winfo_width()
        x_offset = max(0, (canvas_width - self.page_width) // 2)
        
        for page_num in self.loaded_pages:
            y_offset = self.page_offsets.get(page_num, page_num * self.estimated_page_height)
            page = self.doc.load_page(page_num)
            page_height = page.rect.height * self.zoom_level
            
            # Header exclusion zone
            if header > 0:
                self.canvas.create_rectangle(
                    x_offset, y_offset,
                    x_offset + self.page_width, y_offset + header * self.zoom_level,
                    fill="#ff4444", stipple="gray50", outline="#ff0000",
                    tags="margin_preview"
                )
            
            # Footer exclusion zone
            if footer > 0:
                footer_y = y_offset + page_height - footer * self.zoom_level
                self.canvas.create_rectangle(
                    x_offset, footer_y,
                    x_offset + self.page_width, y_offset + page_height,
                    fill="#ff4444", stipple="gray50", outline="#ff0000",
                    tags="margin_preview"
                )
    
    # --- Brightness settings ---
    def show_brightness_settings(self):
        """Show dialog to adjust screen brightness for eye comfort"""
        bright_win = tk.Toplevel(self.root)
        bright_win.title("Brightness")
        bright_win.geometry("350x220")
        bright_win.configure(bg="#2d2d30")
        
        # Header
        tk.Label(bright_win, text="🌙 Screen Brightness", bg="#2d2d30", fg="white",
                font=("Arial", 14, "bold")).pack(pady=10)
        
        tk.Label(bright_win, text="Lower brightness to reduce eye strain", 
                bg="#2d2d30", fg="#aaa", font=("Arial", 10)).pack()
        
        # Brightness slider
        slider_frame = tk.Frame(bright_win, bg="#2d2d30")
        slider_frame.pack(fill=tk.X, padx=20, pady=15)
        
        tk.Label(slider_frame, text="🔅", bg="#2d2d30", fg="white", font=("Arial", 14)).pack(side=tk.LEFT)
        
        brightness_var = tk.DoubleVar(value=self.brightness)
        brightness_scale = tk.Scale(slider_frame, from_=0.3, to=1.0, resolution=0.05,
                                   orient=tk.HORIZONTAL, variable=brightness_var,
                                   bg="#3d3d3d", fg="white", highlightthickness=0, length=200)
        brightness_scale.pack(side=tk.LEFT, padx=10, fill=tk.X, expand=True)
        
        tk.Label(slider_frame, text="🔆", bg="#2d2d30", fg="white", font=("Arial", 14)).pack(side=tk.LEFT)
        
        def apply_brightness(*args):
            self.brightness = brightness_var.get()
            self.db.set_setting("brightness", self.brightness)
            self.rerender_loaded_pages()
        
        brightness_scale.bind("<ButtonRelease-1>", apply_brightness)
        
        # Close button
        ttk.Button(bright_win, text="Close", command=bright_win.destroy).pack(pady=10)
    
    def rerender_loaded_pages(self):
        """Re-render all loaded pages with current brightness"""
        if not self.doc:
            return
        
        # Store current scroll position
        scroll_pos = self.canvas.yview()[0]
        
        # Re-render each loaded page
        for page_num in list(self.loaded_pages):
            page = self.doc.load_page(page_num)
            mat = fitz.Matrix(self.zoom_level, self.zoom_level)
            pix = page.get_pixmap(matrix=mat)
            
            img_data = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            self.page_pil_images[page_num] = img_data.copy()
            
            # Apply brightness
            if self.brightness < 1.0:
                enhancer = ImageEnhance.Brightness(img_data)
                img_data = enhancer.enhance(self.brightness)
            
            photo = ImageTk.PhotoImage(img_data)
            self.page_images[page_num] = photo
            
            y_offset = self.page_offsets.get(page_num, page_num * self.estimated_page_height)
            canvas_width = self.canvas.winfo_width()
            x_offset = max(0, (canvas_width - pix.width) // 2)
            
            self.canvas.delete(f"page_{page_num}")
            self.canvas.create_image(x_offset, y_offset, image=photo, anchor=tk.NW, tags=f"page_{page_num}")
        
        # Restore scroll position
        self.canvas.yview_moveto(scroll_pos)
    
    # --- Progress saving ---
    def save_current_progress(self):
        """Save current reading progress to database"""
        if self.current_pdf_path and self.doc:
            visible_page = self.get_visible_page()
            self.db.update_book_progress(
                self.current_pdf_path,
                visible_page,
                self.current_sentence_idx,
                self.zoom_level
            )
    
    # --- Library view ---
    def show_library(self):
        """Show library window with all books"""
        # Save current progress first
        self.save_current_progress()
        
        # Create library window
        library_win = tk.Toplevel(self.root)
        library_win.title("PDF Library")
        library_win.geometry("800x600")
        library_win.configure(bg="#2d2d30")
        
        # Header
        header = tk.Frame(library_win, bg="#1e1e1e", pady=10)
        header.pack(fill=tk.X)
        tk.Label(header, text="📚 Your Library", bg="#1e1e1e", fg="white",
                font=("Arial", 16, "bold")).pack(side=tk.LEFT, padx=20)
        ttk.Button(header, text="+ Add PDF", command=lambda: self._add_book_to_library(library_win)).pack(side=tk.RIGHT, padx=20)
        
        # Search box
        search_frame = tk.Frame(library_win, bg="#2d2d30", pady=10)
        search_frame.pack(fill=tk.X, padx=20)
        tk.Label(search_frame, text="🔍", bg="#2d2d30", fg="white").pack(side=tk.LEFT)
        search_var = tk.StringVar()
        search_entry = tk.Entry(search_frame, textvariable=search_var, bg="#3d3d3d", fg="white",
                               insertbackground="white", font=("Arial", 11), width=40)
        search_entry.pack(side=tk.LEFT, padx=10, fill=tk.X, expand=True)
        
        # Book list
        list_frame = tk.Frame(library_win, bg="#2d2d30")
        list_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        
        # Scrollable canvas for books
        canvas = tk.Canvas(list_frame, bg="#2d2d30", highlightthickness=0)
        scrollbar = tk.Scrollbar(list_frame, orient=tk.VERTICAL, command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg="#2d2d30")
        
        scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        # Mouse wheel scrolling for library
        def on_library_scroll(event):
            if event.num == 4:  # Linux scroll up
                canvas.yview_scroll(-1, "units")
            elif event.num == 5:  # Linux scroll down
                canvas.yview_scroll(1, "units")
            else:  # Windows/Mac
                canvas.yview_scroll(-1 * (event.delta // 120), "units")
        
        canvas.bind("<Button-4>", on_library_scroll)
        canvas.bind("<Button-5>", on_library_scroll)
        canvas.bind("<MouseWheel>", on_library_scroll)
        scrollable_frame.bind("<Button-4>", on_library_scroll)
        scrollable_frame.bind("<Button-5>", on_library_scroll)
        scrollable_frame.bind("<MouseWheel>", on_library_scroll)
        
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        # Load all books from database (sorted by recently opened)
        all_books = self.db.get_all_books()
        
        def refresh_book_list(*args):
            """Filter and display books based on search query"""
            # Clear existing cards
            for widget in scrollable_frame.winfo_children():
                widget.destroy()
            
            query = search_var.get().lower()
            filtered_books = [b for b in all_books if query in (b['title'] or '').lower() or query in b['path'].lower()]
            
            if not filtered_books:
                if not all_books:
                    msg = "No books in library.\nClick '+ Add PDF' to add your first book."
                else:
                    msg = "No books match your search."
                tk.Label(scrollable_frame, text=msg, bg="#2d2d30", fg="#888", font=("Arial", 12)).pack(pady=50)
            else:
                for book in filtered_books:
                    self._create_book_card(scrollable_frame, book, library_win, on_library_scroll)
        
        # Bind search to key release
        search_var.trace_add("write", refresh_book_list)
        
        # Initial display
        refresh_book_list()
    
    def _create_book_card(self, parent, book, library_win, scroll_handler=None):
        """Create a card for a book in the library with thumbnail"""
        card = tk.Frame(parent, bg="#3d3d3d", pady=10, padx=15)
        card.pack(fill=tk.X, pady=5)
        
        # Generate thumbnail from first page
        thumbnail_label = None
        try:
            if os.path.exists(book['path']):
                doc = fitz.open(book['path'])
                if len(doc) > 0:
                    page = doc.load_page(0)
                    # Render at low resolution for thumbnail
                    mat = fitz.Matrix(0.2, 0.2)  # 20% scale
                    pix = page.get_pixmap(matrix=mat)
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    # Resize to fixed height while maintaining aspect ratio
                    thumb_height = 80
                    aspect = img.width / img.height
                    thumb_width = int(thumb_height * aspect)
                    img = img.resize((thumb_width, thumb_height), Image.Resampling.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                    
                    thumbnail_label = tk.Label(card, image=photo, bg="#3d3d3d")
                    thumbnail_label.image = photo  # Keep reference
                    thumbnail_label.pack(side=tk.LEFT, padx=(0, 15))
                doc.close()
        except Exception as e:
            print(f"Thumbnail error: {e}")
        
        # Book info (truncate long titles/paths)
        title = book['title'] or Path(book['path']).stem
        if len(title) > 50:
            title = title[:47] + "..."
        
        path_display = book['path']
        if len(path_display) > 60:
            path_display = "..." + path_display[-57:]
        
        progress = f"Page {book['last_page'] + 1} of {book['total_pages']}" if book['total_pages'] > 0 else "Not opened yet"
        
        info_frame = tk.Frame(card, bg="#3d3d3d")
        info_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        tk.Label(info_frame, text=title, bg="#3d3d3d", fg="white",
                font=("Arial", 12, "bold"), anchor="w").pack(fill=tk.X)
        tk.Label(info_frame, text=progress, bg="#3d3d3d", fg="#aaa",
                font=("Arial", 10), anchor="w").pack(fill=tk.X)
        tk.Label(info_frame, text=path_display, bg="#3d3d3d", fg="#666",
                font=("Arial", 8), anchor="w").pack(fill=tk.X)
        
        # Buttons
        btn_frame = tk.Frame(card, bg="#3d3d3d")
        btn_frame.pack(side=tk.RIGHT)
        
        ttk.Button(btn_frame, text="Open", 
                  command=lambda p=book['path']: self._open_from_library(p, library_win)).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Remove",
                  command=lambda p=book['path'], c=card: self._remove_from_library(p, c)).pack(side=tk.LEFT)
        
        # Make card clickable and scrollable
        clickable_widgets = [card, info_frame] + list(info_frame.winfo_children())
        if thumbnail_label:
            clickable_widgets.append(thumbnail_label)
        for widget in clickable_widgets:
            widget.bind("<Button-1>", lambda e, p=book['path']: self._open_from_library(p, library_win))
            widget.configure(cursor="hand2")
            # Bind scroll events
            if scroll_handler:
                widget.bind("<Button-4>", scroll_handler)
                widget.bind("<Button-5>", scroll_handler)
                widget.bind("<MouseWheel>", scroll_handler)
    
    def _add_book_to_library(self, library_win):
        """Add a new book to library"""
        filename = filedialog.askopenfilename(filetypes=[("PDF Files", "*.pdf")])
        if filename:
            # Add to database
            doc = fitz.open(filename)
            self.db.add_book(filename, total_pages=len(doc))
            doc.close()
            # Refresh library view
            library_win.destroy()
            self.show_library()
    
    def _open_from_library(self, path, library_win):
        """Open a book from the library"""
        library_win.destroy()
        
        # If it's the same book, just close library - don't reopen
        if path == self.current_pdf_path and self.doc:
            return
        
        self.open_pdf(path)
    
    def _remove_from_library(self, path, card):
        """Remove a book from library"""
        if messagebox.askyesno("Remove Book", "Remove this book from your library?"):
            self.db.remove_book(path)
            card.destroy()
    
    # --- Voice settings ---
    def show_voice_settings(self):
        """Show voice selection dialog"""
        voice_win = tk.Toplevel(self.root)
        voice_win.title("Voice Settings")
        voice_win.geometry("500x400")
        voice_win.configure(bg="#2d2d30")
        
        # Header
        header = tk.Frame(voice_win, bg="#1e1e1e", pady=10)
        header.pack(fill=tk.X)
        tk.Label(header, text="🔊 Select TTS Voice", bg="#1e1e1e", fg="white",
                font=("Arial", 14, "bold")).pack(side=tk.LEFT, padx=20)
        
        # Current voice indicator
        tk.Label(voice_win, text=f"Current: {self.voice}", bg="#2d2d30", fg="#aaa",
                font=("Arial", 10)).pack(pady=5)
        
        # Voice list
        list_frame = tk.Frame(voice_win, bg="#2d2d30")
        list_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        
        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        voice_listbox = tk.Listbox(list_frame, bg="#3d3d3d", fg="white",
                                   selectbackground="#0078d7", selectforeground="white",
                                   font=("Arial", 10), yscrollcommand=scrollbar.set)
        voice_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=voice_listbox.yview)
        
        # Fetch voices in background
        def load_voices():
            try:
                voices = asyncio.run(edge_tts.list_voices())
                # Sort by locale/language
                voices.sort(key=lambda v: (v['Locale'], v['ShortName']))
                
                voice_win.after(0, lambda: populate_list(voices))
            except Exception as e:
                voice_win.after(0, lambda: voice_listbox.insert(tk.END, f"Error loading voices: {e}"))
        
        def populate_list(voices):
            current_locale = ""
            for v in voices:
                locale = v['Locale']
                if locale != current_locale:
                    current_locale = locale
                    voice_listbox.insert(tk.END, f"── {locale} ──")
                
                display_name = f"  {v['ShortName']} ({v['Gender']})"
                voice_listbox.insert(tk.END, display_name)
                
                # Select current voice
                if v['ShortName'] == self.voice:
                    voice_listbox.selection_set(tk.END)
                    voice_listbox.see(tk.END)
        
        voice_listbox.insert(tk.END, "Loading voices...")
        threading.Thread(target=load_voices, daemon=True).start()
        
        # Buttons
        btn_frame = tk.Frame(voice_win, bg="#2d2d30", pady=10)
        btn_frame.pack(fill=tk.X)
        
        def apply_voice():
            selection = voice_listbox.curselection()
            if selection:
                selected_text = voice_listbox.get(selection[0])
                # Skip header lines
                if selected_text.startswith("──") or selected_text == "Loading voices...":
                    return
                # Extract voice name
                voice_name = selected_text.strip().split(" (")[0]
                self.voice = voice_name
                self.db.set_setting("tts_voice", voice_name)
                # Clear audio cache since voice changed
                self.audio_cache.clear()
                messagebox.showinfo("Voice Changed", f"Voice set to: {voice_name}")
                voice_win.destroy()
        
        ttk.Button(btn_frame, text="Apply", command=apply_voice).pack(side=tk.RIGHT, padx=20)
        ttk.Button(btn_frame, text="Cancel", command=voice_win.destroy).pack(side=tk.RIGHT)
    
    # --- App lifecycle ---
    def on_app_close(self):
        """Handle app close - save progress and cleanup"""
        self.save_current_progress()
        self.db.close()
        pygame.mixer.quit()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = VisualEdgeReader(root)
    
    # Handle window close
    root.protocol("WM_DELETE_WINDOW", app.on_app_close)
    
    # Auto-open last book on startup
    last_book = app.db.get_last_opened_book()
    if last_book and os.path.exists(last_book['path']):
        root.after(100, lambda: app.open_pdf(last_book['path']))
    
    root.mainloop()


