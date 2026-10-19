"""Disposable native controls for harness integration tests. No user files."""
import os
import win32api
import win32con
import win32gui


def main():
    instance = win32api.GetModuleHandle(None)
    edit = None
    count = 0

    def wndproc(hwnd, message, wparam, lparam):
        nonlocal count
        if message == win32con.WM_COMMAND and (wparam & 0xffff) == 1002:
            count += 1
            win32gui.SetWindowText(edit, "invoked-" + str(count))
            return 0
        if message == win32con.WM_CLOSE:
            win32gui.DestroyWindow(hwnd)
            return 0
        if message == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, message, wparam, lparam)

    wc = win32gui.WNDCLASS()
    wc.lpfnWndProc = wndproc
    wc.hInstance = instance
    wc.lpszClassName = "HarnessFixture" + str(os.getpid())
    win32gui.RegisterClass(wc)
    hwnd = win32gui.CreateWindow(wc.lpszClassName, "RDC disposable semantic fixture", win32con.WS_OVERLAPPEDWINDOW,
                                 20, 20, 440, 250, 0, 0, instance, None)
    style = win32con.WS_CHILD | win32con.WS_VISIBLE
    edit = win32gui.CreateWindow("EDIT", "before", style | win32con.WS_BORDER, 20, 20, 250, 24, hwnd, 1001, instance, None)
    win32gui.CreateWindow("BUTTON", "Run fixture", style | win32con.BS_PUSHBUTTON, 20, 60, 130, 24, hwnd, 1002, instance, None)
    win32gui.CreateWindow("BUTTON", "Enable fixture", style | win32con.BS_AUTOCHECKBOX, 20, 100, 180, 24, hwnd, 1003, instance, None)
    win32gui.CreateWindow("EDIT", "fixture-private-value", style | win32con.ES_PASSWORD, 20, 140, 150, 24, hwnd, 1004, instance, None)
    win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
    win32gui.UpdateWindow(hwnd)
    win32gui.PumpMessages()


if __name__ == "__main__":
    main()
