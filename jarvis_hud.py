"""
JARVIS HUD - a face for Jarvis (Step 10 of the guide).

Run:   python jarvis_hud.py

A window opens with a glowing reactor core that reacts to what Jarvis is doing: it breathes while it
waits, lights up and pulses with your voice while it listens, turns amber while it thinks and ripples
while it speaks. Around it: live captions of what you said and what Jarvis answered, a clock, and small
read-outs of the brain, ears and voice in use. Everything else (ears, brain, hands, voice and the wake
word) comes from jarvis.py, so all your settings and tools carry over unchanged.

Keys:  Space, or a mouse click  = talk (not needed when WAKE_WORD = True in jarvis.py)
       F                        = full screen on and off
       Esc, or close the window = shut Jarvis down
"""
import math
import os
import random
import sys
import threading
import time

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame

# =====================================================================
#  SETTINGS
# =====================================================================
WINDOW_SIZE = (960, 600)        # width and height in pixels; you can also drag the window edges
FULL_SCREEN = False             # True = fill the whole screen (press F to switch at any time)
ALWAYS_ON_TOP = False           # True = stay above other windows, handy as a small corner widget
ACCENT = (64, 196, 255)         # the Jarvis blue, used for the name, the frame and the captions
FRAMES_PER_SECOND = 30
WORDS_PER_SECOND = 2.6          # how fast the voice talks; the captions light up word by word at this pace

RING_COLOURS = {                # the reactor changes colour with what Jarvis is doing
    "starting": (120, 150, 190),
    "standby": (40, 150, 255),
    "listening": (0, 235, 255),
    "thinking": (255, 176, 48),
    "speaking": (90, 255, 190),
    "off": (110, 110, 125),
}
LABELS = {
    "starting": "STARTING UP",
    "standby": "STANDBY",
    "listening": "LISTENING",
    "thinking": "THINKING",
    "speaking": "SPEAKING",
    "off": "OFFLINE",
}
BACKGROUND = (4, 8, 16)
TEXT = (226, 236, 246)
DIM_TEXT = (110, 135, 165)


# =====================================================================
#  WHAT THE WINDOW KNOWS - jarvis.py reports every step through notify(), we only display it
# =====================================================================
class Status:
    def __init__(self):
        self.state = "starting"
        self.since = time.time()
        self.started = time.time()
        self.you = ""               # the last thing you said
        self.reply = ""             # the last thing Jarvis said
        self.tool = ""              # the tool Jarvis is using right now, if any
        self.level = 0.0            # how loud you are while Jarvis listens (1.0 = the speech threshold)
        self.note = "Loading the ears and the brain, one moment..."
        self.name = "JARVIS"
        self.exchanges = 0          # how many things you have said this session

    def on_event(self, event, value=""):
        if event == "state":
            self.state, self.since = str(value), time.time()
            if value == "listening":
                self.level = 0.0
        elif event == "you":
            self.you, self.reply, self.tool = str(value), "", ""
            self.exchanges += 1
        elif event == "jarvis":
            self.reply = str(value)
        elif event == "tool":
            self.tool = str(value)
        elif event == "level":
            self.level = float(value)
        elif event == "info":
            self.note = str(value)


status = Status()
bridge = {}                     # the worker thread puts the loaded jarvis module here


def run_jarvis():
    """Runs on a background thread: load jarvis.py (ears, brain, voice), then run its main loop."""
    try:
        import jarvis                               # takes a few seconds: the core shows STARTING UP meanwhile
        bridge["jarvis"] = jarvis
        status.name = jarvis.NAME.upper()
        jarvis.listeners.append(status.on_event)
        jarvis.main(jarvis.wait_for_signal)         # Space = talk. With WAKE_WORD = True the name works as well.
    except Exception as error:
        status.on_event("info", f"Jarvis stopped: {error}")
    status.on_event("state", "off")


def talk():
    """Space key or mouse click: tell Jarvis to listen right now."""
    jarvis = bridge.get("jarvis")
    if jarvis is not None:
        jarvis.talk_now.set()


def setting(name, default=""):
    """A setting from jarvis.py, once it has loaded (for the read-outs at the side)."""
    jarvis = bridge.get("jarvis")
    return getattr(jarvis, name, default) if jarvis is not None else default


# =====================================================================
#  DRAWING HELPERS
# =====================================================================
def polar(center, radius, angle):
    return (center[0] + radius * math.cos(angle), center[1] + radius * math.sin(angle))


def shade(colour, factor):
    """The colour, dimmed (factor < 1) or pushed towards white (factor > 1)."""
    if factor <= 1:
        return tuple(max(0, min(255, int(c * factor))) for c in colour[:3])
    return tuple(max(0, min(255, int(c + (255 - c) * (factor - 1)))) for c in colour[:3])


def draw_arc(surface, colour, center, radius, start, end, width):
    steps = max(4, int(abs(end - start) * radius / 8))
    points = [polar(center, radius, start + (end - start) * i / steps) for i in range(steps + 1)]
    pygame.draw.lines(surface, colour, False, points, width)


def wrap(text, font, max_width):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if font.size(trial)[0] <= max_width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


_glow_cache = {}


def glow(colour, radius, strength):
    """A soft round light, drawn once and kept. Blitted additively it lights up whatever is under it."""
    key = (colour, radius, round(strength, 1))
    sprite = _glow_cache.get(key)
    if sprite is None:
        size = radius * 2
        sprite = pygame.Surface((size, size))
        sprite.fill((0, 0, 0))
        steps = 28
        for i in range(steps, 0, -1):
            f = (i / steps)
            brightness = strength * (1 - f) ** 2.2
            pygame.draw.circle(sprite, shade(colour, brightness), (radius, radius), int(radius * f))
        _glow_cache[key] = sprite
    return sprite


def light(screen, colour, center, radius, strength):
    sprite = glow(colour, int(radius), max(0.0, min(1.0, strength)))
    screen.blit(sprite, (int(center[0] - radius), int(center[1] - radius)), special_flags=pygame.BLEND_RGB_ADD)


_backdrop_cache = {}


def backdrop(size):
    """The static background: a faint grid and a soft vignette, drawn once per window size."""
    surface = _backdrop_cache.get(size)
    if surface is None:
        width, height = size
        surface = pygame.Surface(size)
        surface.fill(BACKGROUND)
        grid = shade(ACCENT, 0.09)
        for x in range(0, width, 40):
            pygame.draw.line(surface, grid, (x, 0), (x, height), 1)
        for y in range(0, height, 40):
            pygame.draw.line(surface, grid, (0, y), (width, y), 1)
        light(surface, shade(ACCENT, 0.5), (width // 2, int(height * 0.42)), int(min(width, height) * 0.62), 0.22)
        _backdrop_cache[size] = surface
    return surface


random.seed(7)
PARTICLES = [(random.uniform(1.22, 1.95), random.uniform(-0.5, 0.5), random.uniform(0, math.tau),
              random.choice((1, 1, 2)), random.uniform(0, math.tau)) for _ in range(48)]


_text_cache = {}


def text_image(font, text, colour):
    """Rendered text, kept so the same word is not drawn from scratch thirty times a second."""
    key = (id(font), text, colour)
    image = _text_cache.get(key)
    if image is None:
        if len(_text_cache) > 3000:
            _text_cache.clear()
        image = font.render(text, True, colour)
        _text_cache[key] = image
    return image


def speech_progress():
    """How much of the current reply has been spoken so far, 0 to 1 (estimated from the pace of the voice)."""
    if status.state != "speaking" or not status.reply:
        return 1.0
    words = max(1, len(status.reply.split()))
    return min(1.0, (time.time() - status.since) * WORDS_PER_SECOND / words)


def make_fonts(height):
    _text_cache.clear()

    def font(size, bold=False, mono=False):
        names = "consolas,couriernew,dejavusansmono,monospace" if mono else "segoeui,arial,dejavusans,sans"
        return pygame.font.SysFont(names, max(11, size), bold=bold)
    return {"title": font(height // 16, bold=True), "clock": font(height // 17),
            "label": font(height // 27, bold=True), "body": font(height // 30),
            "small": font(height // 40), "mono": font(height // 40, mono=True), "tiny": font(height // 50, mono=True)}


# =====================================================================
#  THE PICTURE
# =====================================================================
def rhythm(state, t):
    """How the core moves in each state: (beat 0..1 for the glow, wave amplitude, spin speed)."""
    if state == "listening":
        beat = 0.55 + 0.45 * math.sin(t * 5)
        return beat, 0.04 + min(1.0, status.level / 3) * 0.16, 2.2
    if state == "thinking":
        return 0.5 + 0.5 * math.sin(t * 10), 0.05, 4.5
    if state == "speaking":
        return 0.5 + 0.5 * abs(math.sin(t * 12) * math.sin(t * 3.3)), 0.06 + 0.10 * abs(math.sin(t * 7.3) * math.sin(t * 2.1)), 2.6
    if state == "off":
        return 0.15, 0.0, 0.0
    return 0.5 + 0.5 * math.sin(t * 1.3), 0.015, 0.45      # standby / starting: slow breathing


def draw_reactor(screen, fonts, t, center, R):
    state = status.state
    colour = RING_COLOURS.get(state, ACCENT)
    beat, wave, spin = rhythm(state, t)
    cx, cy = center
    age = max(0.0, time.time() - status.since)         # seconds since Jarvis changed state
    loud = min(1.0, status.level / 3) if state == "listening" else 0.0

    # 1. the light behind everything, breathing with the state
    light(screen, colour, center, int(R * 2.1), 0.28 + 0.32 * beat)
    light(screen, colour, center, int(R * 0.95), 0.35 + 0.35 * beat + 0.3 * loud)

    # 1b. the moment Jarvis starts listening, a shockwave races outwards: it heard you
    if state == "listening" and age < 1.0:
        f = age / 1.0
        light(screen, colour, center, int(R * 2.4), 0.8 * (1 - f))
        pygame.draw.circle(screen, shade(colour, 1.4 * (1 - f)), center, int(R * (0.9 + 1.5 * f)), max(1, int(7 * (1 - f))))

    # 1c. sonar rings while listening (faster and brighter the louder you are)
    if state == "listening":
        for k in range(3):
            f = (t * (0.6 + 0.9 * loud) + k / 3) % 1.0
            pygame.draw.circle(screen, shade(colour, (0.2 + 0.9 * loud) * (1 - f)), center, int(R * (0.78 + 1.0 * f)), 1)

    # 1d. sound waves while speaking: they leave the core to the left and to the right
    if state == "speaking":
        for k in range(4):
            f = (t * 1.3 + k / 4) % 1.0
            r = R * (0.95 + 0.9 * f)
            for side in (0.0, math.pi):
                draw_arc(screen, shade(colour, 1.2 * (1 - f)), center, r, side - 0.5, side + 0.5, 2)

    # 2. the core disc and its rim (the disc swells with your voice)
    core = int(R * (0.58 + 0.06 * loud))
    pygame.draw.circle(screen, shade(colour, 0.16 + 0.10 * beat + 0.15 * loud), center, core)
    pygame.draw.circle(screen, shade(colour, 0.9 + 0.3 * beat), center, core, 2)

    # 3. the living waveform ring: the sound made visible
    points = []
    n = 140
    for i in range(n):
        a = i * math.tau / n
        ripple = (0.55 * math.sin(6 * a + 5.0 * t) + 0.30 * math.sin(11 * a - 7.3 * t) + 0.15 * math.sin(17 * a + 3.1 * t))
        points.append(polar(center, R * (0.73 + wave * ripple), a))
    pygame.draw.lines(screen, shade(colour, 1.15), True, points, 2)
    inner = [polar(center, R * (0.655 + wave * 0.5 * math.sin(9 * (i * math.tau / n) - 4 * t)), i * math.tau / n) for i in range(n)]
    pygame.draw.lines(screen, shade(colour, 0.45), True, inner, 1)

    # 4. the main ring
    pygame.draw.circle(screen, shade(colour, 1.05), center, int(R), 3)

    # 5. the segmented ring, a light chasing around it
    segments = 36
    for k in range(segments):
        a0 = k * math.tau / segments + t * 0.35 * spin
        pulse = max(0.0, math.sin(a0 * 2 - t * 2.4 * max(spin, 0.3))) ** 6
        draw_arc(screen, shade(colour, 0.32 + 0.9 * pulse), center, R * 1.12, a0 + 0.012, a0 + math.tau / segments - 0.03, 6)

    # 5b. while speaking, a bright arc grows around the ring as the reply is read out
    if state == "speaking":
        progress = speech_progress()
        if progress > 0.01:
            draw_arc(screen, shade(colour, 1.5), center, R * 1.21, -math.pi / 2, -math.pi / 2 + math.tau * progress, 3)

    # 6. the dashed ring, turning the other way, with one bright sweep
    dashes = 90
    for k in range(dashes):
        a = k * math.tau / dashes - t * 0.5 * spin
        sweep = max(0.0, math.cos(a - t * 1.8 * max(spin, 0.4))) ** 8
        pygame.draw.line(screen, shade(colour, 0.28 + 0.8 * sweep), polar(center, R * 1.29, a), polar(center, R * 1.35, a), 2)

    # 7. two orbits with satellites
    for k, (radius, speed, size) in enumerate(((R * 1.47, 0.9, 4), (R * 1.47, -0.55, 3), (R * 1.58, 0.35, 3))):
        pygame.draw.circle(screen, shade(colour, 0.18), center, int(radius), 1)
        a = t * speed * max(spin, 0.5) + k * 2.1
        pos = polar(center, radius, a)
        light(screen, colour, pos, 18, 0.6)
        pygame.draw.circle(screen, shade(colour, 1.4), (int(pos[0]), int(pos[1])), size)

    # 8. the dial: tick marks and compass numbers
    for k in range(72):
        a = k * math.tau / 72 - math.pi / 2
        big = k % 6 == 0
        r0 = R * 1.66
        pygame.draw.line(screen, shade(colour, 0.95 if big else 0.32), polar(center, r0, a), polar(center, r0 + (14 if big else 6), a), 2 if big else 1)
    for degrees in (0, 90, 180, 270):
        label = fonts["tiny"].render(f"{degrees:03d}", True, shade(colour, 0.7))
        pos = polar(center, R * 1.92, math.radians(degrees) - math.pi / 2)
        screen.blit(label, label.get_rect(center=(int(pos[0]), int(pos[1]))))

    # 9. drifting particles
    for radius, speed, phase, size, twinkle in PARTICLES:
        a = phase + t * speed * (0.5 + 0.5 * max(spin, 0.4))
        pos = polar(center, R * radius, a)
        glimmer = 0.25 + 0.75 * max(0.0, math.sin(t * 1.7 + twinkle)) ** 3
        pygame.draw.circle(screen, shade(colour, 0.35 + 0.9 * glimmer), (int(pos[0]), int(pos[1])), size)

    # 10. the state word in the core, and a small level meter below it
    label = fonts["label"].render(LABELS.get(state, state.upper()), True, shade(colour, 1.5))
    screen.blit(label, label.get_rect(center=(cx, cy - int(R * 0.06))))
    bars = 15
    for i in range(bars):
        x = cx + (i - bars // 2) * 6
        if state == "listening":
            amp = min(1.0, status.level / 3) * (0.35 + 0.65 * abs(math.sin(t * 9 + i * 0.9)))
        elif state == "speaking":
            amp = 0.3 + 0.7 * abs(math.sin(t * 11 + i * 0.7)) * abs(math.sin(t * 2.1 + i * 0.2))
        elif state == "thinking":
            amp = 0.3 + 0.3 * math.sin(t * 6 - i * 0.7)
        else:
            amp = 0.08
        h = max(2, int(amp * R * 0.16))
        pygame.draw.line(screen, shade(colour, 1.1), (x, cy + int(R * 0.22) - h // 2), (x, cy + int(R * 0.22) + h // 2), 3)


def draw_frame(screen, width, height):
    """Thin corner brackets, the signature of every good HUD."""
    c = shade(ACCENT, 0.55)
    m, L = 18, 46
    for x, dx in ((m, 1), (width - m, -1)):
        for y, dy in ((m, 1), (height - m, -1)):
            pygame.draw.line(screen, c, (x, y), (x + dx * L, y), 2)
            pygame.draw.line(screen, c, (x, y), (x, y + dy * L), 2)
            pygame.draw.line(screen, shade(ACCENT, 0.3), (x + dx * 6, y + dy * 6), (x + dx * 16, y + dy * 6), 1)
    # a slow scan line
    y = int((time.time() * 45) % (height + 40)) - 20
    pygame.draw.line(screen, shade(ACCENT, 0.16), (m, y), (width - m, y), 1)


def readout(screen, fonts, x, y, key, value, colour=TEXT):
    k = fonts["tiny"].render(key, True, DIM_TEXT)
    v = fonts["mono"].render(str(value), True, colour)
    screen.blit(k, (x, y))
    screen.blit(v, (x, y + k.get_height()))
    return y + k.get_height() + v.get_height() + 8


def draw_panels(screen, fonts, t, width, height):
    """Header, clock and the read-outs at the sides."""
    colour = RING_COLOURS.get(status.state, ACCENT)
    margin = 34
    # header
    title = fonts["title"].render(" ".join(status.name), True, shade(ACCENT, 1.25))
    screen.blit(title, (margin, 26))
    screen.blit(fonts["small"].render("LOCAL VOICE ASSISTANT", True, DIM_TEXT), (margin + 2, 26 + title.get_height()))
    now = time.localtime()
    clock = fonts["clock"].render(time.strftime("%H:%M:%S", now), True, TEXT)
    screen.blit(clock, (width - clock.get_width() - margin, 26))
    date = fonts["small"].render(time.strftime("%A %d %B %Y").upper(), True, DIM_TEXT)
    screen.blit(date, (width - date.get_width() - margin, 26 + clock.get_height()))

    if width < 760:
        return
    # left read-outs
    y = int(height * 0.30)
    y = readout(screen, fonts, margin, y, "STATUS", LABELS.get(status.state, status.state.upper()), shade(colour, 1.3))
    y = readout(screen, fonts, margin, y, "BRAIN", setting("MODEL", "..."))
    y = readout(screen, fonts, margin, y, "EARS", f"whisper {setting('WHISPER_SIZE', '...')}")
    voice = setting("VOICE_ENGINE", "...")
    y = readout(screen, fonts, margin, y, "VOICE", f"{voice} neural" if voice == "edge" else voice)
    y = readout(screen, fonts, margin, y, "WAKE WORD", "on, say Hey " + status.name.title() if setting("WAKE_WORD", False) else "off, press Space")
    # microphone level bar
    k = fonts["tiny"].render("MIC", True, DIM_TEXT)
    screen.blit(k, (margin, y))
    y += k.get_height() + 4
    level = min(1.0, status.level / 3) if status.state == "listening" else 0.0
    for i in range(12):
        lit = level * 12 > i
        pygame.draw.rect(screen, shade(colour, 1.1) if lit else shade(ACCENT, 0.18), (margin + i * 11, y, 8, 10))
    # right read-outs
    x = width - margin - 150
    y = int(height * 0.30)
    y = readout(screen, fonts, x, y, "SESSION", f"{status.exchanges:02d} exchanges")
    up = int(time.time() - status.started)
    y = readout(screen, fonts, x, y, "UPTIME", f"{up // 3600:02d}:{up % 3600 // 60:02d}:{up % 60:02d}")
    y = readout(screen, fonts, x, y, "LINK", "local only", shade((90, 255, 190), 1.0))
    y = readout(screen, fonts, x, y, "CLOUD", "none")


def caption(screen, fonts, who, text, y, x, max_width, who_colour, text_colour, max_lines, progress=1.0):
    """One caption row: a small name tag on the left, wrapped text on the right. Returns the next y.
    progress < 1 lights the words up one by one as they are spoken; the rest waits in grey."""
    font = fonts["body"]
    screen.blit(text_image(fonts["small"], who, who_colour), (x, y + 4))
    lines = wrap(text, font, max_width - 110)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:max(1, len(lines[-1]) - 3)] + "..."
    total = sum(len(line.split()) for line in lines)
    lit = int(round(progress * total))
    space, line_height = font.size(" ")[0], font.size("Ay")[1]
    seen = 0
    for line in lines:
        cx = x + 100
        for word in line.split():
            image = text_image(font, word, text_colour if seen < lit else DIM_TEXT)
            screen.blit(image, (cx, y))
            cx += image.get_width() + space
            seen += 1
        y += line_height + 2
    return y + 8


def draw(screen, fonts, t):
    width, height = screen.get_size()
    screen.blit(backdrop((width, height)), (0, 0))
    center = (width // 2, int(height * 0.40))
    R = int(min(width, height) * 0.165)
    colour = RING_COLOURS.get(status.state, ACCENT)

    draw_frame(screen, width, height)
    draw_reactor(screen, fonts, t, center, R)
    draw_panels(screen, fonts, t, width, height)

    # under the dial: which tool is running, or (nothing said yet) how to start
    margin = 34
    if status.tool and status.state in ("thinking", "speaking"):
        tool = fonts["mono"].render("using " + status.tool, True, shade(ACCENT, 0.9))
        screen.blit(tool, tool.get_rect(center=(center[0], center[1] + int(R * 2.06))))
    elif status.state == "standby" and not status.you and status.note:
        first_sentence = status.note.split(". ")[0].rstrip(".")
        tip = fonts["body"].render(first_sentence, True, DIM_TEXT)
        screen.blit(tip, tip.get_rect(center=(center[0], center[1] + int(R * 2.06))))

    # captions: what you said, what Jarvis answered
    y = int(height * 0.775)
    pygame.draw.line(screen, shade(ACCENT, 0.25), (margin, y - 8), (width - margin, y - 8), 1)
    if status.you:
        y = caption(screen, fonts, "YOU", status.you, y, margin, width - 2 * margin, DIM_TEXT, TEXT, max_lines=1)
    if status.reply:
        y = caption(screen, fonts, status.name, status.reply, y, margin, width - 2 * margin, shade(colour, 1.2), TEXT,
                    max_lines=3, progress=speech_progress())

    # footer: how to use it (or why it stopped)
    if status.state in ("starting", "off"):
        hint = status.note
    else:
        hint = "SPACE or click = talk     F = full screen     ESC = close"
    foot = fonts["small"].render(hint, True, DIM_TEXT)
    screen.blit(foot, (margin, height - foot.get_height() - 12))


def keep_on_top():
    """Windows only: keep the window above all others."""
    try:
        import ctypes
        hwnd = pygame.display.get_wm_info()["window"]
        ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)    # topmost, keep size and place
    except Exception:
        pass


# =====================================================================
#  THE WINDOW LOOP
# =====================================================================
def main():
    pygame.init()
    full = FULL_SCREEN
    screen = pygame.display.set_mode((0, 0) if full else WINDOW_SIZE,
                                     pygame.FULLSCREEN if full else pygame.RESIZABLE)
    pygame.display.set_caption("Jarvis")
    if ALWAYS_ON_TOP:
        keep_on_top()
    fonts = make_fonts(screen.get_height())
    clock = pygame.time.Clock()

    worker = threading.Thread(target=run_jarvis, daemon=True)
    worker.start()

    started = time.time()
    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_SPACE:
                    talk()
                elif event.key == pygame.K_f:
                    full = not full
                    screen = pygame.display.set_mode((0, 0) if full else WINDOW_SIZE,
                                                     pygame.FULLSCREEN if full else pygame.RESIZABLE)
                    fonts = make_fonts(screen.get_height())
            elif event.type == pygame.MOUSEBUTTONDOWN:
                talk()
            elif event.type == pygame.VIDEORESIZE:
                fonts = make_fonts(event.h)
        draw(screen, fonts, time.time() - started)
        pygame.display.flip()
        clock.tick(FRAMES_PER_SECOND)

    jarvis = bridge.get("jarvis")
    if jarvis is not None:
        jarvis.STOP = True                          # ask the assistant loop to finish...
        worker.join(timeout=3)                      # ...and give it a moment to do so
    pygame.quit()
    sys.stdout.flush()
    os._exit(0)                                     # make sure nothing keeps running in the background


if __name__ == "__main__":
    main()
