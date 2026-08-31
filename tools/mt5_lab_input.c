#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/*
 * Deliberately declare the small Xlib/XTest surface we use so the helper can
 * be built in the diagnostic container, which ships the runtime libraries but
 * no X11 development headers.
 */
typedef struct _XDisplay Display;
typedef unsigned long KeySym;
typedef unsigned char KeyCode;
typedef int Bool;

#define False 0
#define True 1
#define CurrentTime 0L
#define NoSymbol 0L

#define XK_space 0x0020
#define XK_minus 0x002d
#define XK_period 0x002e
#define XK_0 0x0030
#define XK_a 0x0061
#define XK_Shift_L 0xffe1
#define XK_Control_L 0xffe3

extern Display *XOpenDisplay(const char *);
extern int XCloseDisplay(Display *);
extern int XFlush(Display *);
extern KeyCode XKeysymToKeycode(Display *, KeySym);
extern KeySym XStringToKeysym(const char *);
extern Bool XTestFakeKeyEvent(Display *, unsigned int, Bool, unsigned long);
extern Bool XTestFakeButtonEvent(Display *, unsigned int, Bool, unsigned long);
extern Bool XTestFakeMotionEvent(Display *, int, int, int, unsigned long);

static void die(const char *message) {
    fprintf(stderr, "%s\n", message);
    exit(1);
}

static void send_key(Display *display, KeySym symbol, int with_shift) {
    KeyCode key = XKeysymToKeycode(display, symbol);
    KeyCode shift = XKeysymToKeycode(display, XK_Shift_L);

    if (key == 0) {
        die("No X11 keycode for requested symbol");
    }

    if (with_shift) {
        XTestFakeKeyEvent(display, shift, True, CurrentTime);
    }
    XTestFakeKeyEvent(display, key, True, CurrentTime);
    XTestFakeKeyEvent(display, key, False, CurrentTime);
    if (with_shift) {
        XTestFakeKeyEvent(display, shift, False, CurrentTime);
    }
    XFlush(display);
    usleep(20000);
}

static void click(Display *display, int x, int y) {
    XTestFakeMotionEvent(display, 0, x, y, CurrentTime);
    XTestFakeButtonEvent(display, 1, True, CurrentTime);
    XTestFakeButtonEvent(display, 1, False, CurrentTime);
    XFlush(display);
    usleep(150000);
}

static void select_all(Display *display) {
    KeyCode control = XKeysymToKeycode(display, XK_Control_L);
    KeyCode a = XKeysymToKeycode(display, XK_a);

    XTestFakeKeyEvent(display, control, True, CurrentTime);
    XTestFakeKeyEvent(display, a, True, CurrentTime);
    XTestFakeKeyEvent(display, a, False, CurrentTime);
    XTestFakeKeyEvent(display, control, False, CurrentTime);
    XFlush(display);
    usleep(50000);
}

static void type_ascii(Display *display, const char *text) {
    for (const unsigned char *cursor = (const unsigned char *)text; *cursor; cursor++) {
        unsigned char character = *cursor;
        KeySym symbol = NoSymbol;
        int with_shift = 0;

        if (character >= 'a' && character <= 'z') {
            symbol = XK_a + (character - 'a');
        } else if (character >= 'A' && character <= 'Z') {
            symbol = XK_a + (character - 'A');
            with_shift = 1;
        } else if (character >= '0' && character <= '9') {
            symbol = XK_0 + (character - '0');
        } else {
            switch (character) {
                case ' ': symbol = XK_space; break;
                case '-': symbol = XK_minus; break;
                case '.': symbol = XK_period; break;
                case '_': symbol = XK_minus; with_shift = 1; break;
                default: die("Unsupported non-ASCII input character");
            }
        }
        send_key(display, symbol, with_shift);
    }
}

int main(int argc, char **argv) {
    Display *display = XOpenDisplay(NULL);
    if (display == NULL) {
        die("Cannot open X11 display");
    }

    if (argc == 4 && strcmp(argv[1], "click") == 0) {
        click(display, atoi(argv[2]), atoi(argv[3]));
    } else if (argc == 5 && strcmp(argv[1], "replace") == 0) {
        click(display, atoi(argv[2]), atoi(argv[3]));
        select_all(display);
        type_ascii(display, argv[4]);
    } else if (argc == 3 && strcmp(argv[1], "key") == 0) {
        KeySym symbol = XStringToKeysym(argv[2]);
        if (symbol == NoSymbol) {
            die("Unknown X11 key name");
        }
        send_key(display, symbol, 0);
    } else {
        fprintf(stderr,
                "usage: %s click X Y | replace X Y TEXT | key KEYSYM\n",
                argv[0]);
        XCloseDisplay(display);
        return 2;
    }

    XCloseDisplay(display);
    return 0;
}
