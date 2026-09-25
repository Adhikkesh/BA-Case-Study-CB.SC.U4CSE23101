async function pageFunction(context) {
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    await context.waitFor('table tbody tr');          // wait until the JavaScript table is drawn
    await sleep(3000);
    const table = document.querySelector('table');

    // the table is virtualised (only visible rows exist), so find the box that scrolls it
    let box = table.parentElement;
    while (box && box.scrollHeight <= box.clientHeight + 5) box = box.parentElement;
    box = box || document.scrollingElement;

    const rows = new Map();
    let unchanged = 0;
    while (unchanged < 6) {
        const before = rows.size;
        table.querySelectorAll('tbody tr').forEach((tr) => {
            const c = [...tr.querySelectorAll('td')].map((td) => td.innerText.trim());
            if (c.length >= 14 && c[1]) {
                rows.set(c[1], {
                    instance_name: c[0], api_name: c[1], compute_family: c[3],
                    instance_memory: c[5], vcpus: c[6], instance_storage: c[7],
                    network_performance: c[8], linux_on_demand: c[9], linux_spot_min: c[11],
                });
            }
        });
        unchanged = rows.size === before ? unchanged + 1 : 0;
        box.scrollTop += 800;
        await sleep(300);
    }
    context.log.info(`Scraped ${rows.size} instance types`);
    return [...rows.values()];
}
