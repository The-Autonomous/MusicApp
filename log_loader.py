import colorama

colorama.init()

class log_loader:
    def __init__(self, proj_name = "", custom_colors = None, debugging = True):
        self.proj_name = proj_name
        self.do_debug = debugging

        self.default_colors = {
            "INFO": "#00FF00",
            "WARN": "#FFFF00",
            "ERROR": "#FF0000",
            "DEBUG": "#999999"
        }

        self.colors = {**self.default_colors, **(custom_colors or {})}

    def _hex_to_rgb(self, hex_color):
        hex_color = hex_color.lstrip('#')
        return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))

    def cprint(self, *values, color="#FFFFFF", **kwargs):
        r, g, b = self._hex_to_rgb(color)
        ansi_color = f"\033[38;2;{r};{g};{b}m"
        reset = "\033[0m"
        # Fixed the join issue - properly handle multiple values
        txt = " ".join(str(v) for v in values)
        print(f"{ansi_color} {txt} {reset}", **kwargs)

    def print(self, *values, **kwargs):
        self.cprint(f"[{self.proj_name}]", *values, color=self.default_colors["INFO"], **kwargs)
        
    def warn(self, *values, **kwargs):
        self.cprint(f"[{self.proj_name}]", *values, color=self.default_colors["WARN"], **kwargs)
        
    def debug(self, *values, **kwargs):
        if not self.do_debug: return
        self.cprint(f"[{self.proj_name}]", *values, color=self.default_colors["DEBUG"], **kwargs)
        
    def error(self, *values, **kwargs):
        self.cprint(f"[{self.proj_name}]", *values, color=self.default_colors["ERROR"], **kwargs)
        
    def alert(self, color_code, *values, **kwargs):
        """Call a custom default color action for printing"""
        color_final = self.default_colors.get(color_code, self.default_colors["INFO"])
        self.cprint(f"[{self.proj_name}]", *values, color=color_final, **kwargs)