(function () {
  const WIDTH = 2172;
  const HEIGHT = 724;

  function loadImage(src) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = () => reject(new Error('分享卡片底图加载失败'));
      image.src = src;
    });
  }

  function nextFrame() {
    return new Promise(resolve => requestAnimationFrame(() => resolve()));
  }

  async function createQrCanvas(text, size) {
    if (!window.QRCode) {
      throw new Error('二维码组件未加载');
    }
    const holder = document.createElement('div');
    holder.style.position = 'fixed';
    holder.style.left = '-9999px';
    holder.style.top = '-9999px';
    holder.style.width = `${size}px`;
    holder.style.height = `${size}px`;
    document.body.appendChild(holder);
    try {
      new window.QRCode(holder, {
        text,
        width: size,
        height: size,
        colorDark: '#08133e',
        colorLight: '#ffffff',
        correctLevel: window.QRCode.CorrectLevel ? window.QRCode.CorrectLevel.M : undefined
      });
      await nextFrame();
      const canvas = holder.querySelector('canvas');
      if (canvas) {
        return canvas;
      }
      const image = holder.querySelector('img');
      if (image) {
        if (!image.complete) {
          await new Promise(resolve => {
            image.onload = resolve;
            image.onerror = resolve;
          });
        }
        return image;
      }
      throw new Error('二维码生成失败');
    } finally {
      window.setTimeout(() => holder.remove(), 100);
    }
  }

  function roundedRect(ctx, x, y, width, height, radius) {
    const r = Math.min(radius, width / 2, height / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + width, y, x + width, y + height, r);
    ctx.arcTo(x + width, y + height, x, y + height, r);
    ctx.arcTo(x, y + height, x, y, r);
    ctx.arcTo(x, y, x + width, y, r);
    ctx.closePath();
  }

  function drawGlowText(ctx, text, x, y, options = {}) {
    ctx.save();
    ctx.font = options.font || '700 32px "Noto Sans SC", "PingFang SC", sans-serif';
    ctx.fillStyle = options.fill || '#071a5d';
    ctx.textBaseline = options.baseline || 'alphabetic';
    ctx.shadowColor = options.shadowColor || 'rgba(95, 237, 255, 0.68)';
    ctx.shadowBlur = options.shadowBlur ?? 8;
    ctx.shadowOffsetX = 0;
    ctx.shadowOffsetY = 0;
    ctx.fillText(String(text || ''), x, y);
    ctx.restore();
  }

  function ellipsize(ctx, text, maxWidth) {
    const source = String(text || '');
    if (ctx.measureText(source).width <= maxWidth) {
      return source;
    }
    let next = source;
    while (next.length > 0 && ctx.measureText(`${next}...`).width > maxWidth) {
      next = next.slice(0, -1);
    }
    return `${next}...`;
  }

  function middleEllipsize(ctx, text, maxWidth) {
    const source = String(text || '');
    if (ctx.measureText(source).width <= maxWidth) {
      return source;
    }
    const chars = Array.from(source);
    const ellipsis = '……';
    let leftCount = Math.ceil(chars.length / 2);
    let rightCount = chars.length - leftCount;

    while (leftCount + rightCount > 0) {
      const left = chars.slice(0, leftCount).join('');
      const right = rightCount > 0 ? chars.slice(chars.length - rightCount).join('') : '';
      const candidate = `${left}${ellipsis}${right}`;
      if (ctx.measureText(candidate).width <= maxWidth) {
        return candidate;
      }
      if (leftCount > rightCount && leftCount > 1) {
        leftCount -= 1;
      } else if (rightCount > 1) {
        rightCount -= 1;
      } else if (leftCount > 1) {
        leftCount -= 1;
      } else {
        rightCount -= 1;
      }
    }
    return ellipsis;
  }

  function formatNumber(value) {
    const number = Number(value || 0);
    if (!Number.isFinite(number)) {
      return '-';
    }
    if (number >= 10000) {
      return `${(number / 10000).toFixed(1)}万`;
    }
    return number.toLocaleString('zh-CN');
  }

  function normalizeDate(value) {
    const text = String(value || '').trim();
    if (!text) {
      return '-';
    }
    return text.replace(/:\d{2}$/, '');
  }

  function drawInfoRow(ctx, label, value, x, y, valueMaxWidth = 230) {
    ctx.save();
    ctx.textBaseline = 'middle';
    ctx.font = '760 25px "Noto Sans SC", "PingFang SC", sans-serif';
    ctx.fillStyle = '#bdefff';
    ctx.shadowColor = 'rgba(0, 42, 118, 0.95)';
    ctx.shadowBlur = 8;
    ctx.fillText(label, x, y);
    ctx.font = '830 27px "Noto Sans SC", "PingFang SC", sans-serif';
    ctx.fillStyle = '#ecf8ff';
    ctx.shadowColor = 'rgba(0, 42, 128, 0.78)';
    ctx.shadowBlur = 8;
    ctx.fillText(ellipsize(ctx, value || '-', valueMaxWidth), x + 84, y);
    ctx.restore();
  }

  function drawVideoInfo(ctx, videoInfo) {
    const info = videoInfo || {};
    ctx.save();
    ctx.font = '850 35px "Noto Sans SC", "PingFang SC", sans-serif';
    ctx.fillStyle = '#f3fbff';
    ctx.shadowColor = 'rgba(0, 70, 168, 0.82)';
    ctx.shadowBlur = 12;
    const titleX = 120;
    const titleRightX = 760;
    const title = middleEllipsize(ctx, info.title || '未命名视频', titleRightX - titleX);
    ctx.fillText(title, titleX, 333);
    ctx.restore();

    const metaStartY = 382;
    const leftX = 124;
    const rightX = 552;
    const rowGap = 36;
    drawInfoRow(ctx, 'UP主', info.author, leftX, metaStartY, 230);
    drawInfoRow(ctx, '分区', info.type || '-', rightX, metaStartY, 245);
    drawInfoRow(ctx, '发布', normalizeDate(info.publish_time), leftX, metaStartY + rowGap, 255);
    drawInfoRow(ctx, '时长', info.duration, rightX, metaStartY + rowGap, 210);
    drawInfoRow(ctx, '播放', formatNumber(info.view_count), leftX, metaStartY + rowGap * 2, 170);
    drawInfoRow(ctx, '弹幕', formatNumber(info.danmaku_count), rightX, metaStartY + rowGap * 2, 170);
    drawInfoRow(ctx, '点赞', formatNumber(info.like_count), leftX, metaStartY + rowGap * 3, 170);
    drawInfoRow(ctx, '投币', formatNumber(info.coin_count), rightX, metaStartY + rowGap * 3, 170);
    drawInfoRow(ctx, '收藏', formatNumber(info.favorite_count), leftX, metaStartY + rowGap * 4, 170);
    drawInfoRow(ctx, '分享', formatNumber(info.share_count), rightX, metaStartY + rowGap * 4, 170);
  }

  async function drawQr(ctx, shareUrl) {
    if (!shareUrl) {
      return;
    }
    const qrSize = 210;
    const qrCanvas = await createQrCanvas(shareUrl, qrSize);
    const x = 1198;
    const y = 312;
    ctx.save();
    ctx.shadowColor = 'rgba(54, 219, 255, 0.28)';
    ctx.shadowBlur = 10;
    ctx.fillStyle = 'rgba(249, 253, 255, 0.94)';
    roundedRect(ctx, x - 15, y - 15, qrSize + 30, qrSize + 30, 24);
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.strokeStyle = 'rgba(119, 238, 255, 0.56)';
    ctx.lineWidth = 2;
    roundedRect(ctx, x - 15, y - 15, qrSize + 30, qrSize + 30, 24);
    ctx.stroke();
    ctx.drawImage(qrCanvas, x, y, qrSize, qrSize);
    ctx.font = '650 21px "Noto Sans SC", "PingFang SC", sans-serif';
    ctx.fillStyle = 'rgba(238, 250, 255, 0.92)';
    ctx.textAlign = 'center';
    ctx.shadowColor = 'rgba(0, 83, 180, 0.72)';
    ctx.shadowBlur = 5;
    ctx.fillText('扫码查看报告', x + qrSize / 2, y + qrSize + 40);
    ctx.restore();
  }

  async function createReportCardCanvas(options) {
    const background = await loadImage(options.backgroundUrl);
    if (document.fonts && typeof document.fonts.ready?.then === 'function') {
      try {
        await document.fonts.ready;
      } catch (_error) {
        // Fallback fonts are acceptable for export.
      }
    }

    const canvas = document.createElement('canvas');
    canvas.width = WIDTH;
    canvas.height = HEIGHT;
    const ctx = canvas.getContext('2d');
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(background, 0, 0, WIDTH, HEIGHT);

    drawGlowText(ctx, options.bvid || '', 1068, 124, {
      font: '850 47px Manrope, "Noto Sans SC", sans-serif',
      fill: '#f5fbff',
      shadowColor: 'rgba(101, 235, 255, 0.92)',
      shadowBlur: 12
    });
    drawVideoInfo(ctx, options.videoInfo || {});
    await drawQr(ctx, options.shareUrl || '');
    return canvas;
  }

  async function createReportCardBlob(options) {
    const canvas = await createReportCardCanvas(options);
    return new Promise(resolve => {
      canvas.toBlob(blob => resolve(blob), 'image/jpeg', 0.9);
    });
  }

  window.DanmukuReportCard = {
    createReportCardCanvas,
    createReportCardBlob
  };
})();
