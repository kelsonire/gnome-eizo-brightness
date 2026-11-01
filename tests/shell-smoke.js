// Run only in the isolated compositor started by run-shell-test.py.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as Scripting from 'resource:///org/gnome/shell/ui/scripting.js';

export const METRICS = {};

function assert(condition, message) {
    if (!condition)
        throw new Error(message);
}

async function until(predicate) {
    for (let attempt = 0; attempt < 100; attempt++) {
        if (predicate())
            return;
        await Scripting.sleep(50);
    }
    throw new Error('Timed out waiting for the extension');
}

export async function run() {
    const settings = new Gio.Settings({schema_id: 'org.gnome.shell'});
    settings.set_boolean('disable-user-extensions', false);
    Main.extensionManager.enableExtension('eizo-brightness@local');
    await until(() => Main.extensionManager.lookup('eizo-brightness@local')?.stateObj);
    const installed = Main.extensionManager.lookup('eizo-brightness@local');
    assert(installed?.stateObj, `Extension did not load: ${installed?.error}`);
    const extension = installed.stateObj;
    await until(() => !extension._busy);
    assert(Main.panel.statusArea['eizo-brightness@local'], 'Panel icon is missing');

    // Keep this test independent of physical hardware and permissions.
    const originalRun = extension._run;
    const sample = {
        device: '/dev/eizo-ui-test', model: 'EV3240X', serial: 'UI TEST',
        percent: 45, raw: 90, minimum: 0, maximum: 200,
    };
    const writes = [];
    let failWrites = false;
    let statusShown = false;
    let spinnerPlayed = false;
    extension._run = async args => {
        statusShown ||= extension._status.visible;
        await Scripting.sleep(30);
        if (args[0] === 'list')
            return {monitors: [{...sample}], errors: []};
        spinnerPlayed ||= Boolean(extension._rows.get(sample.device).spinner.content);
        writes.push(args);
        if (failWrites)
            throw new Error('UI test: monitor rejected the write');
        sample.percent = Number(args[1]);
        return {...sample};
    };

    await extension._refresh();
    assert(extension._rows.size === 1, 'Monitor slider is missing');
    Main.overview.hide();
    extension._indicator.menu.open();
    await until(() => !extension._busy);
    let row = extension._rows.get(sample.device);
    row.slider.value = 0.3;
    row.slider.value = 0.4;
    row.slider.value = 0.5;
    await until(() => writes.length > 0 && !extension._busy && !extension._debounce);
    assert(writes.length === 1, 'Rapid slider changes were not coalesced');
    assert(writes[0][1] === '50', 'Wrong percentage sent to helper');
    assert(row.value.text === '50%', 'Confirmed brightness was not displayed');
    assert(extension._indicator.menu.isOpen, 'Moving the slider closed the menu');
    assert(!statusShown, 'Status row changed the menu height while working');
    assert(spinnerPlayed, 'Spinner did not run during the write');
    await until(() => !row.spinner.content);

    await Scripting.sleep(250);
    const screenshot = GLib.getenv('EIZO_TEST_SCREENSHOT');
    if (screenshot) {
        const stream = Gio.File.new_for_path(screenshot).replace(
            null, false, Gio.FileCreateFlags.REPLACE_DESTINATION, null);
        try {
            await new Shell.Screenshot().screenshot(false, stream);
        } finally {
            stream.close(null);
        }
    }

    failWrites = true;
    row.slider.value = 0.2;
    await until(() => writes.length === 2 && !extension._busy && !extension._debounce);
    assert(row.slider.value === 0.5, 'Rejected brightness was not rolled back');
    assert(extension._status.label.text.includes('rejected'), 'Write error was hidden');

    // A refresh keeps the rows in place and leaves a slider change made meanwhile alone.
    failWrites = false;
    const refresh = extension._refresh();
    row.slider.value = 0.7;
    await refresh;
    assert(extension._rows.get(sample.device) === row, 'Refresh rebuilt the monitor row');
    assert(row.slider.value === 0.7, 'Refresh overwrote a pending slider change');
    await until(() => writes.length === 3 && !extension._busy && !extension._debounce);
    assert(writes[2][1] === '70', 'Slider change during a refresh was lost');
    assert(!extension._status.visible, 'Refresh did not clear the old error');

    // A late response from the previous enable() must not update a new menu.
    let resolveOld;
    extension._run = () => new Promise(resolve => {
        resolveOld = resolve;
    });
    const oldRefresh = extension._refresh();
    extension.disable();
    extension._run = originalRun;
    extension.enable();
    resolveOld({monitors: [sample], errors: []});
    await oldRefresh;
    await until(() => !extension._busy);
    assert(!extension._rows.has(sample.device), 'Stale async response changed a new menu');
    assert(Main.panel.statusArea['eizo-brightness@local'], 'Re-enabled panel icon is missing');
    print('EIZO shell smoke test: PASS (menu, slider, debounce, errors, lifecycle)');
}
