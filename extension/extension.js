import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import {Spinner} from 'resource:///org/gnome/shell/ui/animation.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Slider} from 'resource:///org/gnome/shell/ui/slider.js';

export default class EizoBrightnessExtension extends Extension {
    enable() {
        this._generation = (this._generation ?? 0) + 1;
        this._alive = true;
        this._busy = false;
        this._refreshPending = false;
        this._debounce = 0;
        this._processes = new Map();
        this._rows = new Map();
        this._pending = new Map();

        this._indicator = new PanelMenu.Button(0.0, 'EIZO Brightness', false);
        const box = new St.BoxLayout({style_class: 'panel-status-menu-box'});
        box.add_child(new St.Icon({
            icon_name: 'display-brightness-symbolic',
            style_class: 'system-status-icon',
        }));
        box.add_child(new St.Label({text: 'EIZO', y_align: Clutter.ActorAlign.CENTER}));
        this._indicator.add_child(box);

        this._indicator.menu.addMenuItem(new PopupMenu.PopupMenuItem(
            'EIZO Display Brightness', {reactive: false}));
        this._section = new PopupMenu.PopupMenuSection();
        this._indicator.menu.addMenuItem(this._section);
        this._status = new PopupMenu.PopupMenuItem('', {reactive: false});
        this._status.label.add_style_class_name('eizo-brightness-status');
        this._status.label.clutter_text.line_wrap = true;
        this._status.visible = false;
        this._indicator.menu.addMenuItem(this._status);
        this._indicator.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._indicator.menu.addAction('Refresh', () => this._refresh());
        this._indicator.menu.connect('open-state-changed', (_menu, open) => {
            if (open)
                this._refresh();
        });
        Main.panel.addToStatusArea(this.uuid, this._indicator);
        this._refresh();
    }

    _setStatus(message) {
        if (!this._alive)
            return;
        this._status.label.text = message;
        this._status.visible = Boolean(message);
    }

    _run(args) {
        const processes = this._processes;
        return new Promise((resolve, reject) => {
            let process;
            try {
                process = Gio.Subprocess.new(
                    ['/usr/bin/python3', `${this.path}/eizo_brightness.py`, ...args, '--json'],
                    Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
            } catch (error) {
                reject(error);
                return;
            }
            let timedOut = false;
            const timeout = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 15000, () => {
                processes.set(process, 0);
                timedOut = true;
                process.force_exit();
                return GLib.SOURCE_REMOVE;
            });
            processes.set(process, timeout);
            process.communicate_utf8_async(null, null, (source, result) => {
                const timer = processes.get(process);
                if (timer)
                    GLib.Source.remove(timer);
                processes.delete(process);
                try {
                    const [, stdout, stderr] = source.communicate_utf8_finish(result);
                    if (timedOut)
                        throw new Error('The monitor is not responding. Check the USB connection.');
                    const data = JSON.parse(stdout);
                    // A list may contain accessible monitors alongside per-device errors.
                    if (!source.get_successful() && !Array.isArray(data.monitors))
                        throw new Error(data.error || stderr.trim() || 'The brightness operation failed.');
                    resolve(data);
                } catch (error) {
                    reject(error);
                }
            });
        });
    }

    async _refresh() {
        if (!this._alive)
            return;
        if (this._busy || this._debounce || this._pending.size) {
            this._refreshPending = true;
            return;
        }
        this._busy = true;
        const generation = this._generation;
        this._refreshPending = false;
        for (const row of this._rows.values())
            row.spinner.play();
        try {
            const data = await this._run(['list']);
            if (!this._alive || generation !== this._generation)
                return;
            this._updateMonitors(data.monitors);
            const errors = data.errors.map(error => error.error);
            this._setStatus(errors.length ? [...new Set(errors)].join('\n') :
                data.monitors.length ? '' : 'No EIZO monitor found. Check the USB connection.');
        } catch (error) {
            if (generation === this._generation)
                this._setStatus(error.message);
        } finally {
            if (this._alive && generation === this._generation) {
                this._busy = false;
                for (const row of this._rows.values())
                    row.spinner.stop();
                this._drain();
            }
        }
    }

    _updateMonitors(monitors) {
        const sameMonitors = monitors.length === this._rows.size && monitors.every(
            monitor => this._rows.get(monitor.device)?.serial === monitor.serial);
        if (!sameMonitors) {
            this._section.removeAll();
            this._rows.clear();
            for (const monitor of monitors)
                this._addMonitor(monitor);
            return;
        }
        // Keep the rows in place so a refresh never moves the open menu.
        for (const monitor of monitors) {
            const row = this._rows.get(monitor.device);
            row.confirmed = monitor.percent;
            if (!this._pending.has(monitor.device))
                this._confirm(row, monitor.percent);
        }
    }

    _addMonitor(monitor) {
        this._section.addMenuItem(new PopupMenu.PopupMenuItem(
            `${monitor.model} · ${monitor.serial}`, {reactive: false}));
        const item = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        const slider = new Slider(monitor.percent / 100);
        slider.x_expand = true;
        slider.accessible_name = `${monitor.model} brightness`;
        slider.add_style_class_name('eizo-brightness-slider');
        // A stopped spinner stays transparent but keeps its space in the row.
        const spinner = new Spinner(16, {animate: true});
        spinner.y_align = Clutter.ActorAlign.CENTER;
        const value = new St.Label({
            text: `${monitor.percent}%`,
            style_class: 'eizo-brightness-value',
            y_align: Clutter.ActorAlign.CENTER,
        });
        item.add_child(slider);
        item.add_child(spinner);
        item.add_child(value);
        // GNOME 50's Slider handles pointer gestures, scrolling, and arrow keys.
        this._section.addMenuItem(item);
        const row = {
            slider, spinner, value, serial: monitor.serial,
            confirmed: monitor.percent, updating: false,
        };
        this._rows.set(monitor.device, row);
        slider.connect('notify::value', () => {
            if (!this._alive || row.updating)
                return;
            const percent = Math.round(slider.value * 200) / 2;
            value.text = `${percent}%`;
            this._pending.set(monitor.device, percent);
            if (this._debounce)
                GLib.Source.remove(this._debounce);
            this._debounce = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 250, () => {
                this._debounce = 0;
                this._drain();
                return GLib.SOURCE_REMOVE;
            });
        });
    }

    _confirm(row, percent) {
        row.updating = true;
        row.confirmed = percent;
        row.slider.value = percent / 100;
        row.value.text = `${percent}%`;
        row.updating = false;
    }

    async _drain() {
        if (!this._alive || this._busy || this._debounce)
            return;
        if (!this._pending.size) {
            if (this._refreshPending)
                this._refresh();
            return;
        }
        this._busy = true;
        const generation = this._generation;
        while (this._alive && generation === this._generation && this._pending.size) {
            const [device, percent] = this._pending.entries().next().value;
            this._pending.delete(device);
            const row = this._rows.get(device);
            row?.spinner.play();
            try {
                const result = await this._run(['set', String(percent), '--device', device]);
                if (this._alive && generation === this._generation) {
                    if (row) {
                        row.spinner.stop();
                        row.confirmed = result.percent;
                        if (!this._pending.has(device))
                            this._confirm(row, result.percent);
                    }
                    this._setStatus('');
                }
            } catch (error) {
                if (!this._alive || generation !== this._generation)
                    return;
                this._setStatus(error.message);
                row?.spinner.stop();
                if (this._alive && row && !this._pending.has(device))
                    this._confirm(row, row.confirmed);
            }
            // Let the latest slider change settle before sending another write.
            if (this._debounce)
                break;
        }
        if (!this._alive || generation !== this._generation)
            return;
        this._busy = false;
        if (this._alive && !this._pending.size && this._refreshPending)
            this._refresh();
    }

    disable() {
        this._alive = false;
        if (this._debounce)
            GLib.Source.remove(this._debounce);
        this._debounce = 0;
        for (const [process, timeout] of this._processes) {
            if (timeout)
                GLib.Source.remove(timeout);
            process.force_exit();
        }
        this._processes.clear();
        this._pending.clear();
        this._rows.clear();
        this._indicator.destroy();
        this._indicator = null;
    }
}
