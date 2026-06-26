from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageTk
import tkinter as tk
from tkinter import filedialog, messagebox


IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".gif",
    ".tif",
    ".tiff",
    ".webp",
}
WHITE_THRESHOLD = 245
MIN_ZOOM = 0.05
MAX_ZOOM = 12.0


class LassoCropApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("圈定区域裁剪工具")
        self.root.geometry("1200x850")
        self.root.minsize(800, 600)

        folder = filedialog.askdirectory(title="选择图片文件夹")
        if not folder:
            self.root.after(0, self.root.destroy)
            return

        self.folder = Path(folder)
        self.out_dir = self.folder / "out"
        self.image_paths = self._find_images(self.folder)
        if not self.image_paths:
            messagebox.showinfo("没有图片", "所选文件夹中没有可处理的图片。")
            self.root.after(0, self.root.destroy)
            return

        self.image_index = 0
        self.original_image: Image.Image | None = None
        self.display_pyramid: list[tuple[float, Image.Image]] = []
        self.display_image: ImageTk.PhotoImage | None = None
        self.image_item: int | None = None
        self.current_path: Path | None = None
        self.crop_counts: dict[Path, int] = {}

        self.zoom = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        self._last_canvas_size = (0, 0)

        self.mode = "pan"
        self.is_panning = False
        self.pan_start = (0, 0)
        self.pan_origin = (0.0, 0.0)

        self.is_lassoing = False
        self.lasso_points_image: list[tuple[float, float]] = []
        self.lasso_line_item: int | None = None
        self.lasso_start_item: int | None = None
        self.lasso_closed_item: int | None = None

        self._build_ui()
        self._bind_events()
        self.root.after(0, self._load_current_image)

    @staticmethod
    def _find_images(folder: Path) -> list[Path]:
        return sorted(
            (
                path
                for path in folder.iterdir()
                if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
            ),
            key=lambda p: p.name.lower(),
        )

    def _build_ui(self) -> None:
        toolbar = tk.Frame(self.root, padx=8, pady=8)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        self.lasso_button = tk.Button(
            toolbar, text="圈定区域 (Q)", width=16, command=self.enter_lasso_mode
        )
        self.lasso_button.pack(side=tk.LEFT, padx=(0, 8))

        self.undo_button = tk.Button(
            toolbar, text="撤销 (Ctrl+Z)", width=16, command=self.undo_lasso
        )
        self.undo_button.pack(side=tk.LEFT, padx=(0, 8))

        self.crop_button = tk.Button(
            toolbar, text="带边界区域裁剪 (E)", width=20, command=self.crop_with_border
        )
        self.crop_button.pack(side=tk.LEFT, padx=(0, 8))

        self.next_button = tk.Button(
            toolbar, text="下一张 (→)", width=16, command=self.next_image
        )
        self.next_button.pack(side=tk.LEFT, padx=(0, 8))

        self.status_var = tk.StringVar()
        status = tk.Label(toolbar, textvariable=self.status_var, anchor="w")
        status.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(12, 0))

        self.canvas = tk.Canvas(self.root, bg="#f3f3f3", highlightthickness=0)
        self.canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    def _bind_events(self) -> None:
        self.root.bind("<q>", lambda _event: self.enter_lasso_mode())
        self.root.bind("<Q>", lambda _event: self.enter_lasso_mode())
        self.root.bind("<e>", lambda _event: self.crop_with_border())
        self.root.bind("<E>", lambda _event: self.crop_with_border())
        self.root.bind("<Control-z>", lambda _event: self.undo_lasso())
        self.root.bind("<Control-Z>", lambda _event: self.undo_lasso())
        self.root.bind("<Right>", lambda _event: self.next_image())

        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<Button-1>", self._on_left_click)
        self.canvas.bind("<B1-Motion>", self._on_left_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_left_release)
        self.canvas.bind("<Motion>", self._on_mouse_move)
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)
        self.canvas.bind("<Button-4>", lambda event: self._zoom_at(event.x, event.y, 1.1))
        self.canvas.bind("<Button-5>", lambda event: self._zoom_at(event.x, event.y, 1 / 1.1))

    def _load_current_image(self) -> None:
        self.root.update_idletasks()
        self.current_path = self.image_paths[self.image_index]
        try:
            self.original_image = Image.open(self.current_path).convert("RGBA")
        except Exception as exc:
            messagebox.showerror("读取失败", f"无法读取图片：{self.current_path.name}\n{exc}")
            self.next_image()
            return

        self.display_pyramid = self._build_display_pyramid(self.original_image)
        self.zoom = self._initial_zoom()
        self.offset_x, self.offset_y = self._center_offsets()
        self._clear_lasso()
        self._render()
        self._update_status()

    def _build_display_pyramid(self, image: Image.Image) -> list[tuple[float, Image.Image]]:
        pyramid = [(1.0, image)]
        scale = 1.0
        current = image

        while min(current.size) > 512:
            scale /= 2
            next_size = (
                max(1, current.width // 2),
                max(1, current.height // 2),
            )
            current = current.resize(next_size, Image.BILINEAR)
            pyramid.append((scale, current))

        return pyramid

    def _initial_zoom(self) -> float:
        if not self.original_image:
            return 1.0
        canvas_w = max(self.canvas.winfo_width(), 1)
        canvas_h = max(self.canvas.winfo_height(), 1)
        img_w, img_h = self.original_image.size
        return min(canvas_w / img_w, canvas_h / img_h, 1.0) * 0.95

    def _center_offsets(self) -> tuple[float, float]:
        if not self.original_image:
            return 0.0, 0.0
        canvas_w = max(self.canvas.winfo_width(), 1)
        canvas_h = max(self.canvas.winfo_height(), 1)
        img_w, img_h = self.original_image.size
        return (
            (canvas_w - img_w * self.zoom) / 2,
            (canvas_h - img_h * self.zoom) / 2,
        )

    def _render(self) -> None:
        if not self.original_image:
            return

        img_w, img_h = self.original_image.size
        display_w = max(1, int(round(img_w * self.zoom)))
        display_h = max(1, int(round(img_h * self.zoom)))
        canvas_w = max(1, self.canvas.winfo_width())
        canvas_h = max(1, self.canvas.winfo_height())

        visible_left = max(0, int(math.floor(self.offset_x)))
        visible_top = max(0, int(math.floor(self.offset_y)))
        visible_right = min(canvas_w, int(math.ceil(self.offset_x + display_w)))
        visible_bottom = min(canvas_h, int(math.ceil(self.offset_y + display_h)))

        if visible_right <= visible_left or visible_bottom <= visible_top:
            preview = Image.new("RGBA", (1, 1), (255, 255, 255, 0))
            preview_x = 0
            preview_y = 0
        else:
            src_left = max(0.0, (visible_left - self.offset_x) / self.zoom)
            src_top = max(0.0, (visible_top - self.offset_y) / self.zoom)
            src_right = min(float(img_w), (visible_right - self.offset_x) / self.zoom)
            src_bottom = min(float(img_h), (visible_bottom - self.offset_y) / self.zoom)

            source_scale, source = self._best_display_source()
            crop_box = (
                max(0, int(math.floor(src_left * source_scale))),
                max(0, int(math.floor(src_top * source_scale))),
                min(source.width, int(math.ceil(src_right * source_scale))),
                min(source.height, int(math.ceil(src_bottom * source_scale))),
            )
            cropped = source.crop(crop_box)
            preview_size = (
                max(1, visible_right - visible_left),
                max(1, visible_bottom - visible_top),
            )
            preview = cropped.resize(preview_size, Image.BILINEAR)
            preview_x = visible_left
            preview_y = visible_top

        self.display_image = ImageTk.PhotoImage(preview)

        if self.image_item is None:
            self.image_item = self.canvas.create_image(
                preview_x, preview_y, anchor=tk.NW, image=self.display_image
            )
        else:
            self.canvas.itemconfigure(self.image_item, image=self.display_image)
            self.canvas.coords(self.image_item, preview_x, preview_y)

        self._redraw_lasso()

    def _best_display_source(self) -> tuple[float, Image.Image]:
        if not self.display_pyramid:
            return 1.0, self.original_image

        for scale, image in reversed(self.display_pyramid):
            if scale >= self.zoom:
                return scale, image

        return self.display_pyramid[0]

    def _position_canvas_items(self) -> None:
        self._render()

    def _update_status(self, extra: str = "") -> None:
        if not self.current_path:
            return
        crop_count = self.crop_counts.get(self.current_path, 0)
        mode_text = "圈定中" if self.mode == "lasso" else "移动/缩放"
        text = (
            f"{self.image_index + 1}/{len(self.image_paths)}  "
            f"{self.current_path.name}  "
            f"缩放 {self.zoom:.0%}  "
            f"已裁剪 {crop_count} 次  "
            f"模式：{mode_text}"
        )
        if extra:
            text = f"{text}  |  {extra}"
        self.status_var.set(text)

    def _canvas_to_image(self, x: float, y: float) -> tuple[float, float]:
        return ((x - self.offset_x) / self.zoom, (y - self.offset_y) / self.zoom)

    def _image_to_canvas(self, x: float, y: float) -> tuple[float, float]:
        return (x * self.zoom + self.offset_x, y * self.zoom + self.offset_y)

    def _point_inside_image(self, x: float, y: float) -> bool:
        if not self.original_image:
            return False
        img_x, img_y = self._canvas_to_image(x, y)
        img_w, img_h = self.original_image.size
        return 0 <= img_x < img_w and 0 <= img_y < img_h

    def _on_canvas_resize(self, event: tk.Event) -> None:
        old_w, old_h = self._last_canvas_size
        if old_w and old_h:
            self.offset_x += (event.width - old_w) / 2
            self.offset_y += (event.height - old_h) / 2
        self._last_canvas_size = (event.width, event.height)
        self._position_canvas_items()

    def _on_mouse_wheel(self, event: tk.Event) -> None:
        factor = 1.1 if event.delta > 0 else 1 / 1.1
        self._zoom_at(event.x, event.y, factor)

    def _zoom_at(self, canvas_x: float, canvas_y: float, factor: float) -> None:
        if not self.original_image:
            return
        old_zoom = self.zoom
        new_zoom = min(MAX_ZOOM, max(MIN_ZOOM, self.zoom * factor))
        if math.isclose(old_zoom, new_zoom):
            return

        img_x, img_y = self._canvas_to_image(canvas_x, canvas_y)
        self.zoom = new_zoom
        self.offset_x = canvas_x - img_x * self.zoom
        self.offset_y = canvas_y - img_y * self.zoom
        self._render()
        self._update_status()

    def _on_left_click(self, event: tk.Event) -> None:
        if self.mode == "lasso":
            self._handle_lasso_click(event.x, event.y)
            return

        self.is_panning = True
        self.pan_start = (event.x, event.y)
        self.pan_origin = (self.offset_x, self.offset_y)
        self.canvas.configure(cursor="fleur")

    def _on_left_drag(self, event: tk.Event) -> None:
        if self.mode == "lasso":
            return
        if not self.is_panning:
            return
        dx = event.x - self.pan_start[0]
        dy = event.y - self.pan_start[1]
        self.offset_x = self.pan_origin[0] + dx
        self.offset_y = self.pan_origin[1] + dy
        self._position_canvas_items()

    def _on_left_release(self, _event: tk.Event) -> None:
        if self.is_panning:
            self.is_panning = False
            self.canvas.configure(cursor="")

    def _on_mouse_move(self, event: tk.Event) -> None:
        if not self.is_lassoing or self.mode != "lasso":
            return
        if not self._point_inside_image(event.x, event.y):
            return

        img_point = self._canvas_to_image(event.x, event.y)
        if self.lasso_points_image:
            prev = self.lasso_points_image[-1]
            if abs(prev[0] - img_point[0]) + abs(prev[1] - img_point[1]) < 1.0:
                return
        self.lasso_points_image.append(img_point)
        self._redraw_lasso(in_progress=True)

    def enter_lasso_mode(self) -> None:
        if not self.original_image:
            return
        self.mode = "lasso"
        self.is_lassoing = False
        self.lasso_points_image = []
        self._delete_lasso_items(keep_closed=True)
        self.canvas.configure(cursor="crosshair")
        self._update_status("单击图片开始圈定，再移动鼠标，第二次单击完成闭合")

    def _handle_lasso_click(self, canvas_x: float, canvas_y: float) -> None:
        if not self._point_inside_image(canvas_x, canvas_y):
            return

        if not self.is_lassoing:
            self._clear_lasso()
            self.is_lassoing = True
            self.mode = "lasso"
            self.lasso_points_image = [self._canvas_to_image(canvas_x, canvas_y)]
            self._redraw_lasso(in_progress=True)
            self._update_status("正在圈定，移动鼠标绘制曲线，第二次单击完成闭合")
            return

        self.lasso_points_image.append(self._canvas_to_image(canvas_x, canvas_y))
        if len(self.lasso_points_image) < 3:
            self._update_status("圈定区域至少需要 3 个点")
            return

        self.is_lassoing = False
        self.mode = "pan"
        self.canvas.configure(cursor="")
        self._redraw_lasso(in_progress=False)
        self._update_status("圈定完成")

    def undo_lasso(self) -> None:
        self._clear_lasso()
        self.mode = "pan"
        self.canvas.configure(cursor="")
        self._update_status("已撤销圈定区域")

    def _clear_lasso(self) -> None:
        self.is_lassoing = False
        self.lasso_points_image = []
        self._delete_lasso_items(keep_closed=False)

    def _delete_lasso_items(self, keep_closed: bool) -> None:
        for attr in ("lasso_line_item", "lasso_start_item"):
            item_id = getattr(self, attr)
            if item_id is not None:
                self.canvas.delete(item_id)
                setattr(self, attr, None)

        if not keep_closed and self.lasso_closed_item is not None:
            self.canvas.delete(self.lasso_closed_item)
            self.lasso_closed_item = None

    def _redraw_lasso(self, in_progress: bool = False) -> None:
        self._delete_lasso_items(keep_closed=False)
        if not self.lasso_points_image:
            return

        canvas_points = [
            coord
            for point in self.lasso_points_image
            for coord in self._image_to_canvas(point[0], point[1])
        ]

        if len(self.lasso_points_image) == 1:
            x, y = self._image_to_canvas(*self.lasso_points_image[0])
            r = 4
            self.lasso_start_item = self.canvas.create_oval(
                x - r, y - r, x + r, y + r, outline="#d00000", width=2
            )
            return

        if in_progress:
            self.lasso_line_item = self.canvas.create_line(
                *canvas_points, fill="#d00000", width=2, dash=(6, 4), smooth=True
            )
        else:
            closed_points = canvas_points + canvas_points[:2]
            self.lasso_closed_item = self.canvas.create_line(
                *closed_points, fill="#d00000", width=2, dash=(6, 4), smooth=True
            )

    def crop_with_border(self) -> None:
        if not self.original_image or not self.current_path:
            return
        if self.is_lassoing or len(self.lasso_points_image) < 3:
            self._update_status("请先完成圈定区域")
            return

        bbox = self._find_non_white_bbox_in_lasso()
        if bbox is None:
            self._update_status("圈定区域内没有非白色像素")
            return

        left, top, right, bottom = bbox
        img_w, _img_h = self.original_image.size
        border = min(left, img_w - 1 - right)
        border = max(0, int(border))

        source_rgb = self._rgba_on_white(self.original_image)
        lasso_mask = self._build_lasso_mask(source_rgb.size)
        masked_source = Image.new("RGB", source_rgb.size, "white")
        masked_source.paste(source_rgb, (0, 0), lasso_mask)
        cropped = masked_source.crop((left, top, right + 1, bottom + 1))
        out_w = cropped.width + border * 2
        out_h = cropped.height + border * 2
        output = Image.new("RGB", (out_w, out_h), "white")
        output.paste(cropped, (border, border))

        self.out_dir.mkdir(exist_ok=True)
        out_path = self._next_output_path(self.current_path)
        output.save(out_path, "JPEG", quality=95)
        self._update_status(f"已输出 {out_path.name}")

    def _find_non_white_bbox_in_lasso(self) -> tuple[int, int, int, int] | None:
        if not self.original_image:
            return None

        image = self._rgba_on_white(self.original_image)
        img_w, img_h = image.size
        polygon = self._lasso_polygon((img_w, img_h))
        mask = self._build_lasso_mask((img_w, img_h))

        xs = [point[0] for point in polygon]
        ys = [point[1] for point in polygon]
        search_left = max(0, min(xs))
        search_right = min(img_w - 1, max(xs))
        search_top = max(0, min(ys))
        search_bottom = min(img_h - 1, max(ys))

        pixels = image.load()
        mask_pixels = mask.load()
        left = img_w
        right = -1
        top = img_h
        bottom = -1

        for y in range(search_top, search_bottom + 1):
            for x in range(search_left, search_right + 1):
                if not mask_pixels[x, y]:
                    continue
                if self._is_non_white(pixels[x, y]):
                    left = min(left, x)
                    right = max(right, x)
                    top = min(top, y)
                    bottom = max(bottom, y)

        if right < left or bottom < top:
            return None
        return left, top, right, bottom

    def _build_lasso_mask(self, size: tuple[int, int]) -> Image.Image:
        polygon = self._lasso_polygon(size)
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).polygon(polygon, outline=255, fill=255)
        return mask

    def _lasso_polygon(self, size: tuple[int, int]) -> list[tuple[int, int]]:
        img_w, img_h = size
        return [
            (
                int(round(min(max(x, 0), img_w - 1))),
                int(round(min(max(y, 0), img_h - 1))),
            )
            for x, y in self.lasso_points_image
        ]

    @staticmethod
    def _rgba_on_white(image: Image.Image) -> Image.Image:
        if image.mode != "RGBA":
            return image.convert("RGB")
        background = Image.new("RGBA", image.size, "white")
        background.alpha_composite(image)
        return background.convert("RGB")

    @staticmethod
    def _is_non_white(pixel: tuple[int, int, int]) -> bool:
        red, green, blue = pixel
        return red < WHITE_THRESHOLD or green < WHITE_THRESHOLD or blue < WHITE_THRESHOLD

    def _next_output_path(self, input_path: Path) -> Path:
        count = self.crop_counts.get(input_path, 0)
        stem = input_path.stem

        while True:
            count += 1
            candidate = self.out_dir / f"{stem}_{count}.jpg"
            if not candidate.exists():
                self.crop_counts[input_path] = count
                return candidate

    def next_image(self) -> None:
        if not self.image_paths:
            return
        if self.image_index >= len(self.image_paths) - 1:
            self._update_status("已经是最后一张图片")
            return
        self.image_index += 1
        self.image_item = None
        self.canvas.delete("all")
        self._load_current_image()


def main() -> None:
    root = tk.Tk()
    LassoCropApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
