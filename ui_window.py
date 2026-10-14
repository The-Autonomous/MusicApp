"""Shared borderless cards, without importing player or input hooks."""


def style_card(window, drag_handle=None):
    window.overrideredirect(True)
    window.attributes("-topmost", True)
    if drag_handle is None:
        return
    position = []

    def start(event):
        position[:] = [event.x_root, event.y_root, window.winfo_x(), window.winfo_y()]

    def move(event):
        if position:
            x = position[2] + event.x_root - position[0]
            y = position[3] + event.y_root - position[1]
            window.geometry(f"{x:+d}{y:+d}")

    drag_handle.configure(cursor="fleur")
    drag_handle.bind("<Button-1>", start)
    drag_handle.bind("<B1-Motion>", move)
