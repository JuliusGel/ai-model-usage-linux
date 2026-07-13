/* AI Usage Indicator — GNOME Shell panel widget.
 *
 * Pure presentation: it reads ~/.cache/ai-usage-indicator/state.json (written by the
 * Python backend service) and renders each provider inline as [name][bar][percent],
 * colored by pressure, with a details popup. It never talks to any API itself.
 */

import St from 'gi://St';
import Clutter from 'gi://Clutter';
import GObject from 'gi://GObject';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const STATE_PATH = GLib.build_filenamev(
    [GLib.get_user_cache_dir(), 'ai-usage-indicator', 'state.json']);

const TRACK_WIDTH = 46; // px; fill width is a fraction of this
const REREAD_SECONDS = 15; // fallback poll + refreshes the "updated ago" text

const PRESSURE_COLOR = {
    'normal': '#2ecc71',
    'warning': '#f1c40f',
    'near-limit': '#e74c3c',
    'unknown': '#95a5a6',
};

const AiUsagePanel = GObject.registerClass(
class AiUsagePanel extends PanelMenu.Button {
    _init(extPath) {
        super._init(0.0, 'AI Usage Indicator', false);
        this._extPath = extPath;

        this._panelBox = new St.BoxLayout({
            style_class: 'aui-panel',
            y_align: Clutter.ActorAlign.CENTER,
        });
        this.add_child(this._panelBox);

        this._state = null;

        // React instantly to backend writes, plus a slow poll as a fallback.
        this._monitor = Gio.File.new_for_path(STATE_PATH)
            .monitor(Gio.FileMonitorFlags.NONE, null);
        this._monitorId = this._monitor.connect('changed', () => this._reload());
        this._timeoutId = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT, REREAD_SECONDS, () => {
                this._reload();
                return GLib.SOURCE_CONTINUE;
            });

        this._reload();
    }

    _readState() {
        try {
            const [ok, bytes] = GLib.file_get_contents(STATE_PATH);
            if (!ok)
                return null;
            const text = new TextDecoder().decode(bytes);
            return JSON.parse(text);
        } catch (_e) {
            return null;
        }
    }

    _makeChip(provider) {
        const chip = new St.BoxLayout({
            style_class: 'aui-chip',
            y_align: Clutter.ActorAlign.CENTER,
        });

        // Provider icon (icons/<id>.svg) if present, else fall back to the initial letter.
        const iconPath = GLib.build_filenamev([this._extPath, 'icons', `${provider.id}.svg`]);
        if (this._extPath && GLib.file_test(iconPath, GLib.FileTest.EXISTS)) {
            const icon = new St.Icon({
                gicon: Gio.icon_new_for_string(iconPath),
                style_class: 'aui-icon',
                y_align: Clutter.ActorAlign.CENTER,
            });
            icon.set_icon_size(16);
            chip.add_child(icon);
        } else {
            const initial = (provider.display_name || '?').substring(0, 1).toUpperCase();
            chip.add_child(new St.Label({
                text: initial,
                style_class: 'aui-name',
                y_align: Clutter.ActorAlign.CENTER,
            }));
        }

        const color = PRESSURE_COLOR[provider.pressure] || PRESSURE_COLOR['unknown'];
        const pct = Number.isFinite(provider.percent) ? provider.percent : null;

        const track = new St.BoxLayout({
            style_class: 'aui-track',
            y_align: Clutter.ActorAlign.CENTER,
        });
        const fillWidth = pct === null ? 0 : Math.round(TRACK_WIDTH * Math.max(0, Math.min(100, pct)) / 100);
        const fill = new St.Widget({style_class: 'aui-fill'});
        fill.set_style(`background-color: ${color}; width: ${fillWidth}px;`);
        track.add_child(fill);
        chip.add_child(track);

        chip.add_child(new St.Label({
            text: provider.error ? '!' : (pct === null ? '—' : `${pct}%`),
            style_class: 'aui-pct',
            y_align: Clutter.ActorAlign.CENTER,
        }));

        return chip;
    }

    _rebuildPanel() {
        this._panelBox.destroy_all_children();

        if (!this._state || !this._state.providers || this._state.providers.length === 0) {
            this._panelBox.add_child(new St.Label({
                text: 'AI …',
                style_class: 'aui-name',
                y_align: Clutter.ActorAlign.CENTER,
            }));
            return;
        }
        for (const provider of this._state.providers)
            this._panelBox.add_child(this._makeChip(provider));
    }

    _agoText() {
        if (!this._state || !this._state.updated_at)
            return 'never updated';
        const now = Math.floor(GLib.get_real_time() / 1e6);
        const secs = Math.max(0, now - this._state.updated_at);
        if (secs < 60)
            return `updated ${secs}s ago`;
        if (secs < 3600)
            return `updated ${Math.round(secs / 60)}m ago`;
        return `updated ${Math.round(secs / 3600)}h ago`;
    }

    _rebuildMenu() {
        this.menu.removeAll();

        if (!this._state || !this._state.providers || this._state.providers.length === 0) {
            const item = new PopupMenu.PopupMenuItem('Backend not running');
            item.setSensitive(false);
            this.menu.addMenuItem(item);
            const hint = new PopupMenu.PopupMenuItem('Start: systemctl --user start ai-usage-indicator');
            hint.setSensitive(false);
            this.menu.addMenuItem(hint);
        } else {
            for (const provider of this._state.providers) {
                const row = new PopupMenu.PopupMenuItem(provider.detail || provider.display_name);
                row.setSensitive(false);
                this.menu.addMenuItem(row);
                if (provider.reset_text) {
                    const reset = new PopupMenu.PopupMenuItem(`    ${provider.reset_text}`);
                    reset.setSensitive(false);
                    this.menu.addMenuItem(reset);
                }
            }
        }

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        const ago = new PopupMenu.PopupMenuItem(this._agoText());
        ago.setSensitive(false);
        this.menu.addMenuItem(ago);

        const refresh = new PopupMenu.PopupMenuItem('Refresh now');
        refresh.connect('activate', () => this._refreshNow());
        this.menu.addMenuItem(refresh);
    }

    _refreshNow() {
        // Ask the backend for a one-shot fetch; the file monitor picks up the new state.
        try {
            const proc = Gio.Subprocess.new(
                ['ai-usage-indicator', '--once'],
                Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE);
            proc.wait_async(null, null);
        } catch (e) {
            logError(e, 'ai-usage-indicator: could not run "ai-usage-indicator --once"');
        }
    }

    _reload() {
        this._state = this._readState();
        this._rebuildPanel();
        this._rebuildMenu();
    }

    destroy() {
        if (this._timeoutId) {
            GLib.Source.remove(this._timeoutId);
            this._timeoutId = null;
        }
        if (this._monitor) {
            if (this._monitorId)
                this._monitor.disconnect(this._monitorId);
            this._monitor.cancel();
            this._monitor = null;
        }
        super.destroy();
    }
});

export default class AiUsageIndicatorExtension extends Extension {
    enable() {
        console.log('[ai-usage-indicator] enable build=2 (provider icons)');
        this._indicator = new AiUsagePanel(this.path);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
