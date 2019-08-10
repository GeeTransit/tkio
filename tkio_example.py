import tkinter
from tkinter import colorchooser

import tkio

events = []
errors = []

async def test(wait):
    import random

    tk = await tkio.get_tk()
    canvas = tkinter.Canvas(tk, highlightthickness=0)
    canvas.pack(expand=True, fill="both")
    last = None

    def random_colour():
        return "#" + "".join(hex(random.getrandbits(4) + 8)[2] for _ in range(6))

    async def clear(colour):
        canvas.delete(tkinter.ALL)
        canvas["bg"] = colour

    x = y = None
    colour = random_colour()

    try:

        while True:
            e = await tkio.pop_event()
            events.append(e)
            et = str(e.type)
            if et in {"Motion", "Enter"}:
                if x is None:
                    x, y = e.x, e.y
                canvas.create_line(e.x, e.y, x, y, fill=colour, width=2)
                x, y = e.x, e.y
                canvas.create_oval(x - 1, y - 1, x + 1, y + 1, outline=colour)
            elif et == "ButtonPress":
                last = None
                if e.num == 3:
                    canvas.delete("all")
                elif e.num == 1:
                    if new := colorchooser.askcolor()[1]:
                        colour = new
            elif et == "KeyPress":
                if last:
                    text = canvas.itemcget(last, "text")
                    canvas.itemconfig(last, text=text + e.char)
                else:
                    last = canvas.create_text(e.x, e.y, text=e.char, anchor="sw")
            else:
                last = None

    except tkio.CloseWindow:
        pass
    if wait:
        while True:
            await tkio.sleep(5)



if __name__ == "__main__":
    with tkio.TkLoop() as tkl:
        tkl.run(tkio.new_task(test(True)))
        tkl.run(test(False))
