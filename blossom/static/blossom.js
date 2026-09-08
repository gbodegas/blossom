/*
  Two enhancements, and both pages work with this file absent.

  The first is for the forms that make a plan: say that a plan is being made,
  keep saying so as the minutes pass, and let one press be one request from
  that form. The messages carry no closing punctuation; the pulsing dots after
  them are it. Only forms marked data-pending take part, and their submit
  buttons carry no name, so disabling one changes nothing about what is sent.
  Forms whose buttons carry a decision or a step are left alone. The later
  messages come from timers in this page, not from the server, so they say
  only that the wait goes on.

  The second opens a folded disclosure when a link on the page points inside
  it, so "What is shared" shows what it names.
*/
(function () {
  "use strict";

  var forms = document.querySelectorAll("form[data-pending]");
  var originalTitle = document.title;

  function say(form, words) {
    var note = form.querySelector(".pending");
    if (!note) {
      note = document.createElement("p");
      note.className = "pending";
      note.setAttribute("role", "status");
      note.setAttribute("aria-live", "polite");
      form.appendChild(note);
    }
    note.textContent = "";
    var text = document.createElement("span");
    text.textContent = words;
    var dots = document.createElement("span");
    dots.className = "dots";
    dots.setAttribute("aria-hidden", "true");
    for (var i = 0; i < 3; i += 1) {
      dots.appendChild(document.createElement("i"));
    }
    note.appendChild(text);
    note.appendChild(dots);
  }

  function reset(form) {
    delete form.dataset.busy;
    form.removeAttribute("aria-busy");
    (form.pendingTimers || []).forEach(clearTimeout);
    form.pendingTimers = [];
    var button = form.querySelector("button[type=submit]");
    if (button) {
      button.disabled = false;
      if (button.dataset.label) {
        button.textContent = button.dataset.label;
      }
    }
    var note = form.querySelector(".pending");
    if (note) {
      note.remove();
    }
    document.title = originalTitle;
  }

  Array.prototype.forEach.call(forms, function (form) {
    form.addEventListener("submit", function (event) {
      if (form.dataset.busy) {
        event.preventDefault();
        return;
      }
      form.dataset.busy = "1";
      form.setAttribute("aria-busy", "true");
      var button = form.querySelector("button[type=submit]");
      if (button) {
        button.dataset.label = button.textContent;
        button.disabled = true;
        button.textContent = form.dataset.pending;
      }
      say(form, form.dataset.pending);
      document.title = form.dataset.pending;
      form.pendingTimers = [];
      if (form.dataset.pendingLater) {
        form.pendingTimers.push(setTimeout(function () {
          say(form, form.dataset.pendingLater);
        }, 20000));
      }
      if (form.dataset.pendingMuchLater) {
        form.pendingTimers.push(setTimeout(function () {
          say(form, form.dataset.pendingMuchLater);
        }, 60000));
      }
    });
  });

  /* A page restored from the browser's cache comes back as it was left, busy
     included; the controls are handed back. */
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      Array.prototype.forEach.call(forms, reset);
    }
  });

  document.addEventListener("click", function (event) {
    var link = event.target.closest ? event.target.closest("a[href^='#']") : null;
    if (!link) {
      return;
    }
    var target = document.querySelector(link.getAttribute("href"));
    var fold = target ? target.closest("details") : null;
    if (fold) {
      fold.open = true;
    }
  });
})();
