import assert from "node:assert/strict";

import { selectTaskFromSearch } from "../src/task-query.mjs";

const tasks = [{ id: 7 }, { id: 12 }];

assert.equal(selectTaskFromSearch("?task=12", tasks), 12);
assert.equal(selectTaskFromSearch("", tasks), null);
assert.equal(selectTaskFromSearch("?task=abc", tasks), null);
assert.equal(selectTaskFromSearch("?task=99", tasks), null);
assert.equal(selectTaskFromSearch("?task=9007199254740992", tasks), null);
assert.equal(selectTaskFromSearch("?task=12.0", tasks), null);
assert.equal(selectTaskFromSearch("?task=-12", tasks), null);

console.log("task query selection tests passed");
